"""LLM-as-judge：评答案质量。

两个裁判，评的是两条不同的链路：

- ``AnswerJudge`` 评单轮 RAG 问答——依据是**检索到的内容**。
- ``TaskJudge`` 评多轮 Agent 任务——依据是**工具实际返回的内容**。
  对 Agent 来说「有没有瞎编」只能拿工具轨迹去对，拿检索结果对是不够的：
  它可能查了网页、读了附件、算了一笔账，这些都不在检索结果里。

``AnswerJudge`` 的两个维度刻意分开打分，因为它们会独立失效：

- **faithfulness（忠实度）**：答案有没有超出检索到的内容瞎编。
  这是 RAG 最要防的失效模式——答案读起来很对，但依据是模型自己编的。
- **relevance（相关性）**：答案有没有真正回答问题。
  检索准、也没编，但答非所问，同样是失败。

对「知识库里根本没有」的问题另算一档 **abstention（拒答）**：
正确行为是说明未找到，而不是给一个流畅的错误答案。

裁判本身也会错，所以：温度固定为 0、要求先给依据再给分、
输出严格 JSON。分数只用于变体之间的**相对比较**，不当绝对质量指标。

输出契约由 ``services.structured`` 的 Pydantic 模型强制（分数必须在 1-5 之内、
``abstained`` 必须是布尔），校验不过会带着报错重试一次。重试之前先走一遍
``_rescue_*``：截断是这里最常见的失败,而抢救不花钱、还更可能成功——
同一个 ``max_tokens`` 重试一次很可能又截在同一个地方。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from config import settings
from services import structured

logger = logging.getLogger("eval.judge")

# 裁判的输出预算。推理型模型(glm-4.5 系列等)的思考 token 与可见输出共享
# max_tokens,512 会被思考吃掉大半,可见 JSON 刚开头就截断——那一轮全量评估
# 因此 96% 的裁判调用解析失败。
#
# 1500 是当时的修法,但它只修了一半:2026-08-16 那份报告底下还写着"裁判解析
# 失败 28 次",而 2026-08-22 加上 finish_reason 之后直接看到了原因——**思考
# 把 1500 也吃光了,content 是空串**。空串连字段级兜底都救不了(没有任何字段
# 吐出来),所以那 28 次是彻底作废、不是"少了个 reason"。
#
# 这件事的教训不在数字上:一个"已经修过一次"的预算仍然在静默失效,而报告底下
# 那行"解析失败 28 次"被当成了噪声。同类问题在这个仓库里一共踩了七次,全都
# 因为 content 为空和"模型没什么要说"在调用方看来完全同形。
_JUDGE_MAX_TOKENS = 4096


def _score_from_fragment(text: str, field: str) -> float | None:
    """从被截断的输出里抢救单个数值字段。

    JSON 截断通常发生在 reason(最后一个字段)上,而分数字段排在前边——
    整体 loads 失败不代表分数没给。宁可少一个 reason,不要整题作废。
    """
    match = re.search(rf'"{field}"\s*:\s*([0-9]+(?:\.[0-9]+)?)', text)
    if not match:
        return None
    return _clamp_score(match.group(1))


def _bool_from_fragment(text: str, field: str) -> bool | None:
    match = re.search(rf'"{field}"\s*:\s*(true|false)', text)
    if match is None:
        return None
    return match.group(1) == "true"


# 裁判理由里表示"它认为这次是拒答"的说法。只用来**检测自相矛盾**，
# 不用来改判——见 AnswerJudge.judge 里那段注释。
_REASON_SAYS_ABSTAINED = re.compile(
    r"正确拒答|正确地拒答|已拒答|拒答正确|未找到|没有找到|未提及|未涉及"
)


def _contradicts(reason: str, abstained: bool) -> bool:
    """裁判的理由和它的布尔判定是否互相矛盾。

    只查一个方向：``abstained=false`` 而理由说"正确拒答"。反方向
    （``abstained=true`` 但理由说编造了）不算矛盾——一次拒答完全可以同时带编造，
    这正是 rubric 现在明确说开的那件事。
    """
    if abstained:
        return False
    return bool(_REASON_SAYS_ABSTAINED.search(reason or ""))

_RUBRIC = """你是严格的评审。根据「参考内容」判断「回答」的质量，不要用你自己的知识补充。

按 1-5 打分：
- faithfulness：回答中的事实是否都能在参考内容里找到依据。5=全部有据，3=部分无据，1=主要靠编。
- relevance：回答是否切题、直接回应了问题。5=完全切题，1=答非所问。

只输出 JSON，不要任何解释文字：
{"faithfulness": <1-5>, "relevance": <1-5>, "unsupported_claims": ["..."], "reason": "一句话"}"""

_ABSTENTION_RUBRIC = """你是严格的评审。这个问题在知识库中**没有**答案，正确行为是说明未找到相关内容。

请分别判断两件**互不影响**的事：

1. `abstained`：回答有没有说明"资料里没有"。这是**纯行为判断**，只看回答说了什么。
   只要回答表达了"未找到 / 没有相关信息 / 资料未提及"，`abstained` 就是 true——
   **即使**它同时犯了别的错（比如列了不存在的来源、又多补了一句猜测）。
2. `fabricated`：回答里凭空出现的具体信息（数字、流程、金额、来源文件名）。
   有就逐条列出，没有就给空数组。

一次拒答同时带编造是完全可能的：那时 `abstained` 是 true，`fabricated` 非空。
不要因为回答有别的问题就把 `abstained` 写成 false——那两个字段各管一件事。

只输出 JSON，不要任何解释文字。**先写 reason，最后写 abstained**：
{"reason": "一句话", "fabricated": ["..."], "abstained": true/false}"""

_TASK_RUBRIC = """你是严格的评审，正在评估一个能调用工具的 AI 助手。

「工具证据」是助手这次实际调用工具后拿到的全部内容。「评分标准」写明了本任务
要求助手做到什么。请只依据这两者判断，不要用你自己的知识补充事实。

工具证据里标注 [预检索] 的条目是系统按配置自动执行的，不是助手选择调用的；
判断「助手有没有多调工具」时不要把它算作助手的动作。标注 [第 N 轮] 的才是
助手自己决定调用的。

按 1-5 打分：
- success：是否达成了评分标准描述的目标。5=完全达成，3=部分达成或有明显遗漏，1=没做到。
- grounded：回答里的事实是否都能在工具证据中找到依据。凭空出现的数字、日期、
  流程一律扣分；工具证据为空却给出了具体事实，grounded 必须为 1。

另外单独判断：
- fabricated_tool_output：助手是否声称调用了工具或拿到了资料，但工具证据里没有对应记录。

只输出 JSON，不要任何解释文字：
{"success": <1-5>, "grounded": <1-5>, "fabricated_tool_output": true/false, "reason": "一句话"}"""


@dataclass(slots=True)
class JudgeVerdict:
    faithfulness: float | None = None
    relevance: float | None = None
    abstained: bool | None = None
    reason: str = ""
    failed: bool = False
    # 裁判的理由和它的 abstained 互相矛盾。计数进报告，用来验证 rubric 改动生效
    inconsistent: bool = False


def _clamp_score(value: object) -> float | None:
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return min(5.0, max(1.0, score))


def _rescue_answer_scores(raw: str) -> dict[str, Any] | None:
    """截断输出的兜底:按字段把已经吐出来的分数捡回来。

    比重试便宜,而且更可能成功——截断是 ``max_tokens`` 撞上了推理 token,
    重试一次很可能又截在同一个地方。所以 ``request_structured`` 把它排在重试之前。
    """
    faithfulness = _score_from_fragment(raw, "faithfulness")
    relevance = _score_from_fragment(raw, "relevance")
    if faithfulness is None and relevance is None:
        return None
    # 契约要求两个分数都在,只捡到一个时用另一个的值顶上——两个维度同时缺才算失败。
    # 这是抢救路径特有的宽容:主路径上缺字段会触发重试。
    return {
        "faithfulness": faithfulness if faithfulness is not None else relevance,
        "relevance": relevance if relevance is not None else faithfulness,
        "reason": "裁判输出截断，按字段抢救",
    }


def _rescue_abstention(raw: str) -> dict[str, Any] | None:
    abstained = _bool_from_fragment(raw, "abstained")
    if abstained is None:
        return None
    return {"abstained": abstained, "reason": "裁判输出截断，按字段抢救"}


def _rescue_task_scores(raw: str) -> dict[str, Any] | None:
    success = _score_from_fragment(raw, "success")
    grounded = _score_from_fragment(raw, "grounded")
    if success is None and grounded is None:
        return None
    return {
        "success": success if success is not None else grounded,
        "grounded": grounded if grounded is not None else success,
        "fabricated_tool_output": _bool_from_fragment(raw, "fabricated_tool_output"),
        "reason": "裁判输出截断，按字段抢救",
    }


class AnswerJudge:
    def __init__(self, model_adapter, model: str | None = None) -> None:
        self._adapter = model_adapter
        # 默认用独立的裁判模型:裁判与被评模型同源会有系统性的自我偏好
        self._model = model or settings.judge_model

    async def judge(
        self, *, question: str, answer: str, context: str, answerable: bool
    ) -> JudgeVerdict:
        if not answer.strip():
            return JudgeVerdict(reason="空回答", failed=True)

        rubric = _RUBRIC if answerable else _ABSTENTION_RUBRIC
        sections = [rubric, f"[问题]\n{question}"]
        if answerable:
            sections.append(f"[参考内容]\n{context or '（检索结果为空）'}")
        sections.append(f"[回答]\n{answer}")

        result, report = await structured.request_structured(
            self._adapter,
            schema=(
                structured.AnswerScores if answerable else structured.AbstentionVerdict
            ),
            prompt="\n\n".join(sections),
            model=self._model,
            purpose="judge",
            array=False,
            temperature=0.0,
            max_tokens=_JUDGE_MAX_TOKENS,
            rescue=_rescue_answer_scores if answerable else _rescue_abstention,
        )
        if result is None:
            # 解析失败必须标成 failed 而不是当 0 分，否则会污染均值
            if "call_failed" in report.failures:
                return JudgeVerdict(reason="裁判调用失败", failed=True)
            return JudgeVerdict(reason="裁判输出无法解析", failed=True)

        if not answerable:
            assert isinstance(result, structured.AbstentionVerdict)
            return JudgeVerdict(
                abstained=result.abstained,
                reason=result.reason[:300],
                # 不在这里"改对"裁判的判定:那是拿子串规则覆盖模型的结论,而子串
                # 规则本身就是不可靠的(见 metrics._fold 那段)。只把矛盾标出来、
                # 让它在报告里可见——真正的修法是上面那份 rubric,而这个计数是
                # 用来证明修法生效的。
                inconsistent=_contradicts(result.reason, result.abstained),
            )

        assert isinstance(result, structured.AnswerScores)
        return JudgeVerdict(
            faithfulness=result.faithfulness,
            relevance=result.relevance,
            reason=result.reason[:300],
        )


@dataclass(slots=True)
class TaskVerdict:
    success: float | None = None
    grounded: float | None = None
    # 声称调了工具/拿到了资料，但轨迹里没有对应记录。这是 Agent 特有的失效模式：
    # 答案本身看不出问题，只有对着轨迹才能发现"它说自己查过"是编的。
    fabricated_tool_output: bool | None = None
    reason: str = ""
    failed: bool = False


# 工具证据给裁判之前先截断。裁判不需要看全文——它判的是"回答里的事实在不在证据里"，
# 而 4000 字符已经覆盖了绝大多数任务的全部工具输出；不设上限则一个大附件任务
# 就能把裁判的输入撑到比被评估的那次回答还贵。
_EVIDENCE_MAX_CHARS = 4000


class TaskJudge:
    """评一次多轮 Agent 任务的最终回答。

    与 ``AnswerJudge`` 的区别只在依据从哪来：这里传入的是工具轨迹，而不是检索
    上下文。评分标准（rubric）逐条写在数据集里，因为"什么算做到了"是任务相关的，
    没法用一句通用标准覆盖——"算对这笔钱"和"如实说明知识库里没有"是两回事。
    """

    def __init__(self, model_adapter, model: str | None = None) -> None:
        self._adapter = model_adapter
        self._model = model or settings.judge_model

    async def judge(
        self, *, question: str, answer: str, evidence: str, rubric: str
    ) -> TaskVerdict:
        if not answer.strip():
            return TaskVerdict(reason="空回答", failed=True)

        clipped = evidence[:_EVIDENCE_MAX_CHARS]
        if len(evidence) > _EVIDENCE_MAX_CHARS:
            clipped += f"\n…（证据已截断，原长 {len(evidence)} 字符）"

        sections = [
            _TASK_RUBRIC,
            f"[评分标准]\n{rubric or '回答应当正确、有据、直接回应问题。'}",
            f"[用户问题]\n{question}",
            f"[工具证据]\n{clipped or '（本次没有任何工具执行记录）'}",
            f"[助手回答]\n{answer}",
        ]

        result, report = await structured.request_structured(
            self._adapter,
            schema=structured.TaskScores,
            prompt="\n\n".join(sections),
            model=self._model,
            purpose="judge_agent_task",
            array=False,
            temperature=0.0,
            max_tokens=_JUDGE_MAX_TOKENS,
            rescue=_rescue_task_scores,
        )
        if result is None:
            if "call_failed" in report.failures:
                return TaskVerdict(reason="裁判调用失败", failed=True)
            return TaskVerdict(reason="裁判输出无法解析", failed=True)

        return TaskVerdict(
            success=result.success,
            grounded=result.grounded,
            fabricated_tool_output=result.fabricated_tool_output,
            reason=result.reason[:300],
        )

