"""结构化输出:用 Pydantic 约束模型的返回,校验失败时把报错回灌让它自己改。

这个项目里有四处要求模型返回 JSON:多查询改写(字符串数组)、listwise 重排
(编号数组)、记忆抽取(对象数组)、LLM 裁判(打分对象)。改造之前每一处都是
「正则抠出第一个 JSON → ``json.loads`` → 手写 isinstance 逐字段过一遍」,于是:

- 同样的抠取逻辑抄了三遍(``_JSON_ARRAY_RE`` / ``_JSON_OBJECT_RE``),
  三份正则还不完全一样;
- 校验是"能过就过":重排返回 ``[1, 2, "三"]`` 时那个字符串被静默丢掉,
  返回 ``[99]`` 时越界项被丢掉,结果是**一个看起来正常的空重排**;
- 失败只有降级,没有重试。而这些失败绝大多数是格式问题(裹了解释文字、
  漏了引号、被 max_tokens 截断),模型看一眼报错就能改对。

三个设计决定:

1. **Pydantic 负责契约,不负责宽容。** 类型不对就是不对——静默丢掉不合法的项
   等于把"模型答错了"翻译成"这次没什么可做的",而后者不会有人去查。
2. **重试把 ``ValidationError`` 的原文回灌给模型。** 和工具参数校验失败时回灌
   ``INVALID_ARGUMENTS`` 是同一个套路(见 ``tool_runtime``):错误信息本身就是
   最好的修正指令,比在提示词里预先叮嘱十句管用。次数由
   ``STRUCTURED_OUTPUT_RETRIES`` 控制,默认 1。
3. **失败不抛异常,返回 None 交给调用方降级。** 这四处全是**增强**:改写失败就用
   原查询、重排失败就用融合序、抽取失败就少记一条记忆。把异常抛到 Agent 循环里
   会让一个可选的优化变成回答失败的原因。裁判是唯一例外——它失败必须标成
   ``failed`` 而不是当 0 分,否则会污染均值,那个判断留在 ``eval/judge`` 里。

埋点只记尝试次数与失败类型,不记模型输出正文——沿用"attributes 只存元数据"的约定。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from config import settings
from services.telemetry import current_span

logger = logging.getLogger("structured")

T = TypeVar("T", bound=BaseModel)

# 抠 JSON 用的两条正则。贪婪匹配到最后一个闭合符号:模型爱在 JSON 前后加解释文字,
# 也爱用 ```json 围栏包起来,而非贪婪版本遇到嵌套结构会在第一个内层 ``}`` 就停下。
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def extract_json(text: str, *, array: bool) -> Any | None:
    """从模型输出里抠出第一个 JSON 值。抠不出或解析失败返回 None。

    ``array`` 决定找数组还是对象。分开传而不是"两个都试":调用方知道自己要什么,
    而一个返回了对象的"数组契约"是真的错了,不该被当成成功。
    """
    if not text:
        return None
    pattern = _JSON_ARRAY_RE if array else _JSON_OBJECT_RE
    match = pattern.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


@dataclass(slots=True)
class StructuredReport:
    """一次结构化请求的过程记录。

    ``attempts`` / ``failures`` / ``rescued`` 是元数据,``_record`` 只写这几项进埋点。
    ``last_raw`` 保存最后一次的模型输出正文,供调用方做兜底解析——它**不进埋点**
    (沿用"attributes 只存元数据"的约定),放在这里只是为了不让调用方为了拿一段
    原文而绕开这个入口自己再发一次请求。
    """

    attempts: int = 0
    # 每次失败的原因标签:call_failed / no_json / invalid / truncated
    failures: list[str] = field(default_factory=list)
    # 靠 rescue 回调救回来的(而不是模型直接给对的)
    rescued: bool = False
    last_raw: str = ""
    # 最后一次调用的终止原因。``truncated`` 与 ``no_json`` 会同时出现:
    # 前者说明**为什么**抠不出 JSON,而这个区别决定了该改预算还是改提示词。
    finish_reason: str | None = None

    @property
    def budget_exhausted(self) -> bool:
        """预算被吃光:撞到 max_tokens 且一个字都没输出。

        这是混合推理模型特有的失败形状——思考先花预算,不够时 content 是空串。
        和"输出太长被截断"要分开:后者还有内容可以 rescue,前者什么都没有。
        """
        return self.finish_reason == "length" and not self.last_raw.strip()

    @property
    def ok(self) -> bool:
        return self.attempts > len(self.failures) or self.rescued

    @property
    def retried(self) -> bool:
        return self.attempts > 1


# ---- 各调用点的契约 ----------------------------------------------------------
#
# 放在这里而不是各自的模块里:它们是"模型必须返回什么"的声明,和提示词是一对。
# 摆在一起才能一眼看出四处的契约风格是否一致,也避免 retriever 里放一个 Pydantic
# 模型、memory_service 里放另一个,下次加第五处时不知道该照哪个抄。


class QueryVariants(BaseModel):
    """多查询改写:一组不同措辞的检索语句。"""

    # 模型只被要求输出裸数组,所以这个模型是给 ``root`` 用的包装(见
    # ``parse_root_array``)。字段名不进提示词,不必和输出对齐。
    items: list[str]

    @field_validator("items")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("改写结果里没有任何非空查询")
        return cleaned


class RerankOrder(BaseModel):
    """listwise 重排:候选片段的编号,按相关度从高到低。"""

    items: list[int]

    @field_validator("items")
    @classmethod
    def _positive(cls, value: list[int]) -> list[int]:
        # 只校验"是正整数编号",不校验上界:上界取决于本次给了几个候选,
        # 那是调用方的信息,放进模型会让这个契约和某一次调用绑死。
        if any(item < 1 for item in value):
            raise ValueError("片段编号必须是从 1 开始的正整数")
        if not value:
            raise ValueError("重排结果为空")
        return value


class QueryRoute(BaseModel):
    """查询路由:这个查询偏字面匹配还是偏语义相似。

    ``lexical`` 该偏 BM25(精确编号、API 名、错别字),``semantic`` 该偏稠密向量
    (改述式提问、同义表述),``mixed`` 两路各半。

    这个分类器的准确率是**可测的**:eval 数据集的 ``probe`` 标注
    (lexical / paraphrase / table_lookup ...)天生就是它的标注集,所以不必凭感觉
    判断路由有没有用——把路由结果记进 span,事后和 probe 对一遍就行。
    """

    intent: Literal["lexical", "semantic", "mixed"]

    @property
    def dense_weight(self) -> float:
        if self.intent == "lexical":
            return settings.RAG_ROUTE_WEAK_WEIGHT
        return settings.RAG_RRF_DENSE_WEIGHT

    @property
    def sparse_weight(self) -> float:
        if self.intent == "semantic":
            return settings.RAG_ROUTE_WEAK_WEIGHT
        return settings.RAG_RRF_SPARSE_WEIGHT


class HypotheticalAnswer(BaseModel):
    """HyDE:一段编出来的假答案,只用来做稠密检索。

    ``answer`` 的事实正确性**完全无关**——它从不进上下文、也不给用户看,唯一的
    作用是提供一段措辞更接近文档的向量化输入。所以这里只校验"非空、别太短",
    不校验内容。
    """

    answer: str

    @field_validator("answer")
    @classmethod
    def _substantial(cls, value: str) -> str:
        cleaned = (value or "").strip()
        # 太短的"假答案"（比如模型回一句"好的"）拿去检索比原查询更差:
        # 它既没有原查询的关键词,也没有文档的措辞
        if len(cleaned) < 10:
            raise ValueError("假答案过短,拿它检索会比原查询更差")
        return cleaned


class MemoryItem(BaseModel):
    kind: str
    content: str

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        kind = value.strip()
        if kind not in ("fact", "preference"):
            raise ValueError("kind 只能是 fact 或 preference")
        return kind

    @field_validator("content")
    @classmethod
    def _bounded(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("content 不能为空")
        # 上限在契约里而不是在调用方的 if 里:超长的"记忆"多半是把整段对话抄了
        # 一遍,那是模型没照指令做,应该让它重试一次,而不是静默跳过这一条。
        if len(content) > settings.MEMORY_ITEM_MAX_CHARS:
            raise ValueError(
                f"content 超过 {settings.MEMORY_ITEM_MAX_CHARS} 字，"
                "请只写一条可复用的事实或偏好，不要复述对话"
            )
        return content


class MemoryItems(BaseModel):
    items: list[MemoryItem]


class PlanStep(BaseModel):
    """显式规划里的一步。

    ``tool`` 允许空串:比较两处规定、汇总、下结论这类步骤没有对应工具,硬要求
    每步都填一个会直接鼓励模型乱调。工具名的**合法性**不在这里查——可用工具是
    调用方才知道的(它随开关和委派模式变),所以那一层校验留给 planner。
    """

    goal: str
    tool: str = ""

    @field_validator("goal")
    @classmethod
    def _bounded_goal(cls, value: str) -> str:
        goal = value.strip()
        if not goal:
            raise ValueError("goal 不能为空")
        # 上限在契约里:一个"步骤"写到两百字就不是步骤了,是把整段回答提前写完。
        # 那属于模型没照指令做,应该重试一次,而不是静默接受一个假计划。
        if len(goal) > 200:
            raise ValueError("goal 超过 200 字，请只写这一步要得到什么")
        return goal


class Plan(BaseModel):
    """一份计划。空列表是合法的——见 prompts/agent_plan/。

    步数上限由 ``AGENT_PLAN_MAX_STEPS`` 强制,而且是**校验**而不是截断:
    多出来的步骤直接砍掉等于把"模型没照 max_steps 做"翻译成"计划就这么长",
    而前者该让它重写一次(和 MemoryItem 超长时的取舍一致)。
    """

    items: list[PlanStep]

    @field_validator("items")
    @classmethod
    def _bounded_length(cls, value: list[PlanStep]) -> list[PlanStep]:
        limit = max(1, settings.AGENT_PLAN_MAX_STEPS)
        if len(value) > limit:
            raise ValueError(
                f"最多 {limit} 步，收到 {len(value)} 步；请合并成更少的步骤"
            )
        return value


class AnswerScores(BaseModel):
    """``AnswerJudge`` 的可答问题评分。"""

    faithfulness: float = Field(ge=1, le=5)
    relevance: float = Field(ge=1, le=5)
    unsupported_claims: list[str] = Field(default_factory=list)
    reason: str = ""


class AbstentionVerdict(BaseModel):
    """``AnswerJudge`` 的不可答问题判定。"""

    abstained: bool
    fabricated: list[str] = Field(default_factory=list)
    reason: str = ""


class TaskScores(BaseModel):
    """``TaskJudge`` 的多轮任务评分。"""

    success: float = Field(ge=1, le=5)
    grounded: float = Field(ge=1, le=5)
    fabricated_tool_output: bool | None = None
    reason: str = ""


# ---- 请求 -------------------------------------------------------------------


def _retry_message(error: Exception, *, array: bool) -> str:
    """回灌给模型的纠正说明。

    带上 ``ValidationError`` 的原文:它已经指明了是哪个字段、哪一项、为什么不合法,
    重写一遍只会更模糊。同时重申一次形状要求——被截断的那次输出模型自己也看不到
    截在哪,只说"你错了"它无从下手。
    """
    shape = "JSON 数组" if array else "JSON 对象"
    return (
        f"上一次的输出不符合要求，校验报错如下：\n{error}\n\n"
        f"请重新输出，只给一个合法的 {shape}，不要任何解释文字、不要代码围栏。"
    )


async def request_structured(
    adapter: Any,
    *,
    schema: type[T],
    prompt: str,
    model: str,
    purpose: str,
    array: bool,
    temperature: float = 0.0,
    # 默认值曾经是 512,而它是个陷阱:混合推理模型先花预算思考,512 在实测里
    # 正好落在"返回空串"那一侧(见 scripts/probe_structured_budgets.py)。所有
    # 调用方现在都显式传值,这个默认值只对**下一个**调用方生效——所以它必须
    # 站在安全的一侧,而不是省钱的一侧。
    max_tokens: int = 2048,
    retries: int | None = None,
    rescue: Callable[[str], dict[str, Any] | None] | None = None,
) -> tuple[T | None, StructuredReport]:
    """要一份符合 ``schema`` 的结构化输出,校验失败时重试。

    ``array=True`` 时模型被要求输出裸数组,抠到的数组会包成 ``{"items": [...]}``
    再交给 ``schema`` 校验——提示词让模型输出 ``["a","b"]`` 比输出
    ``{"items":["a","b"]}`` 的成功率高得多,而 Pydantic 侧多包一层几乎没有成本。

    ``rescue`` 是**重试之前**的免费兜底:给它最后一次的原始输出,它按调用方自己的
    规则拼一个 dict 出来(典型是从被 ``max_tokens`` 截断的 JSON 里按字段捡回已经
    吐出来的值)。顺序是有意的——截断的输出重试一次很可能又在同一个地方截断,
    而抢救不花钱。抢救回来的结果同样要过 ``schema`` 校验,不走后门。

    返回 ``(实例或 None, 过程记录)``。调用方按 None 走自己的降级路径。
    """
    limit = settings.STRUCTURED_OUTPUT_RETRIES if retries is None else retries
    report = StructuredReport()
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    for attempt in range(max(1, limit + 1)):
        report.attempts += 1
        try:
            completion = await adapter.complete(
                messages=messages,
                tools=[],
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                purpose=purpose,
            )
        except Exception as exc:
            logger.warning("%s call failed: %s", purpose, type(exc).__name__)
            report.failures.append("call_failed")
            # 调用本身失败(超时、限流、鉴权)重试同一段提示词没有意义:
            # 错不在输出格式上。直接放弃,让调用方降级。
            break

        raw = completion.content or ""
        report.last_raw = raw
        report.finish_reason = getattr(completion, "finish_reason", None)
        payload = extract_json(raw, array=array)
        if payload is None:
            error: Exception = ValueError(
                f"输出里找不到合法的 {'JSON 数组' if array else 'JSON 对象'}"
            )
            report.failures.append("no_json")
            if report.budget_exhausted:
                # 单独标一个标签:``no_json`` 会让人去改提示词,而这里该改的是
                # max_tokens。两者的修法完全不同,混在一个标签里等于没有信息。
                report.failures.append("truncated")
                logger.warning(
                    "%s: max_tokens=%s exhausted before any output — "
                    "the model spent the whole budget thinking. This call site "
                    "degrades silently; raise its budget instead of editing the prompt.",
                    purpose,
                    max_tokens,
                )
        else:
            try:
                value = schema.model_validate(
                    {"items": payload} if array else payload
                )
            except ValidationError as exc:
                error = exc
                report.failures.append("invalid")
            else:
                _record(purpose, report)
                return value, report

        if rescue is not None:
            salvaged = rescue(raw)
            if salvaged is not None:
                try:
                    value = schema.model_validate(salvaged)
                except ValidationError:
                    pass
                else:
                    report.rescued = True
                    _record(purpose, report)
                    return value, report

        if attempt >= limit:
            break
        if report.budget_exhausted:
            # 和 call_failed 同理:重试解决不了预算问题。而且更糟——回灌的
            # assistant 空消息 + 纠正说明会占掉输入,下一次思考的余地只会更小。
            break
        # 把这一轮的输出和纠正说明一起接在后面。带上模型自己那句话是必要的:
        # 只发纠正说明的话它看不到"上一次"指的是什么。
        messages.append({"role": "assistant", "content": raw})
        messages.append(
            {"role": "user", "content": _retry_message(error, array=array)}
        )

    _record(purpose, report)
    return None, report


def _record(purpose: str, report: StructuredReport) -> None:
    """把过程记进当前 span。

    重试次数是要盯的成本项:某一处的 ``retried`` 长期不为 0,说明该改提示词或者
    换模型,而不是继续多付一次调用。
    """
    if report.attempts <= 1 and not report.failures:
        return
    current_span().set(
        **{
            f"structured.{purpose}.attempts": report.attempts,
            f"structured.{purpose}.failures": report.failures or None,
            f"structured.{purpose}.rescued": report.rescued or None,
            # 预算耗尽单独占一列:它和其他失败的区别是"改配置能修",
            # 而报告里看到这一列非零就说明有个调用点在静默降级。
            f"structured.{purpose}.budget_exhausted": (
                True if report.budget_exhausted else None
            ),
        }
    )
