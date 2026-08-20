"""Agent 端到端评估执行器。

一次运行的形状：

    对每个变体：
        （确保语料、伪用户、附件夹具就位）
        对每个任务：
            新建一个一次性对话
            对任务里的每一轮：
                驱动真实的 stream_ai_response，收集它吐出的事件流
                算工具决策指标 / 轮次效率 / 关键词命中 / canary
            把最后一轮的回答连同工具轨迹交给 TaskJudge 打分
            删掉这个对话，以及 Agent 写进知识库的文档
        汇总成一行

和 ``runner.py``（RAG 问答链路）的分工写在那边的模块说明里：检索改动的效果必须
在没有多轮决策噪声的地方量，Agent 的效果必须在真的走多轮的地方量。两套评估
共用语料、伪用户与价目表，其余完全分开。

五个关键取舍：

1. **驱动真实的 ``stream_ai_response``，不在这里重写一个循环。** 重写一个就变成
   "评估自己的循环写得对不对"，而预检索、轨迹回灌、工具结果预算、护栏收集、
   最后一轮不下发 schema 这些逻辑全都不会被覆盖到。代价是这套评估需要真实数据库
   和真实的 chats/messages 行——所以每个任务用一次性对话，跑完就删。

2. **round 0（预检索）不计入工具决策。** 它是配置决定的，不是模型选的。混进
   工具精度里会让"开了预检索"看起来像"模型很会用工具"，而关掉预检索的变体
   反而显得更差。它单独记一列。

3. **任务的单位是多轮对话，不是单个问题。** ``memory`` 探针的第二轮只有在第一轮
   的工具轨迹被回灌之后才答得上来——这正是第一步做的事，而此前没有任何数字
   量过它。数据集的形状因此必须是 ``turns``。

6. **长期记忆靠预置，不靠真实抽取。** 抽取挂在 ``chat_router`` 的异步触发上，
   这套评估直接驱动 ``chat_service``，那条路不会跑；而即便接进来，"这句话该不该
   记成记忆"由辅助模型决定，注入防线的对照组每次形状都会不同。所以任务可以声明
   ``seed_memories`` 直接写行，量的是"记忆已经在库里了，模型会不会照它说的做"。
   代价要说清楚：抽取侧那层防线（把针对助手行为的要求排除在 preference 之外）
   不在这套评估的覆盖范围内，它只有单元测试。

4. **温度固定 0.0。** 产品默认 0.7，但那会让同一个变体跑两次得到不同的工具序列，
   变体之间的差异被方差盖掉。代价要说清楚：这里量的是贪心决策路径，
   不是平均行为；线上真实表现会比这更抖。

5. **成本与 token 从 ``trace_spans`` 反查，不自己数。** 那张表是应用自己写的，
   顺带验证了埋点链路；自己在评估里数一遍，只能证明评估会数数。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import inspect

from config import settings
from database import SessionLocal, engine
from eval import agent_metrics, agent_stubs, metrics
from eval.agent_variants import AgentVariant
from eval.judge import TaskJudge, TaskVerdict
from eval.runner import (
    CORPUS_DIR,
    EVAL_USER_ID,
    _eval_workspace_id,
    ensure_corpus,
    ensure_eval_user,
)
from models import Document, MessageToolStep, TraceSpan, UserMemory
from services import prompt_library
from services.chat_service import ChatService
from services.clock import naive_now
from services.knowledge_service import KnowledgeService
from services.model_adapter import OpenAICompatibleAdapter
from services.retrieval_index import invalidate_scope_indexes
from services.workspace_tools import WRITE_NAME_PREFIX
from services.workspace_tools import enabled_names as workspace_enabled_names

logger = logging.getLogger("eval.agent_runner")

DATASET_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "datasets", "agent_tasks.jsonl"
)

# 模型自己的工具（round >= 1）之外，预检索用的是同一个工具名。区分靠轮次，不靠名字。
PREFETCH_ROUND = 0


@dataclass(slots=True)
class TurnSpec:
    """任务里的一轮。期望值标在轮上而不是任务上——多轮任务里每轮该做的事不同。"""

    question: str
    # 这一轮必须至少各调一次的工具（模型自主调用，不含预检索）
    expect_tools: list[str] = field(default_factory=list)
    # 允许但不要求的工具。只影响精度、不影响召回——否则"多查一次知识库"这种
    # 完全合理的动作会被算成噪声，而模型本来就该有一定的自由度。
    allow_tools: list[str] = field(default_factory=list)
    # 明确不该调的工具。写操作未经用户要求就被调用属于这一类
    forbid_tools: list[str] = field(default_factory=list)
    # expect_tools 是否必须按给定次序出现（子序列）
    expect_order: bool = False
    must_include: list[str] = field(default_factory=list)
    # 命中即失败。注入样本的 canary 靠它判定
    must_avoid: list[str] = field(default_factory=list)
    # 一个正确回答至少需要几轮模型调用。工具零次 = 1；调一次工具再作答 = 2。
    # 标错了会让轮次效率变成一个看起来精确的假数字，所以数据集里逐条标。
    min_rounds: int = 1


@dataclass(slots=True)
class AgentTask:
    id: str
    probe: str
    # 「什么算做到了」，逐任务写给裁判看
    rubric: str
    turns: list[TurnSpec]
    use_rag: bool = True
    # 搜索通道行为：ok / empty / fail，见 agent_stubs
    stub_mode: str = "ok"
    title: str = ""
    # 跑这个任务之前预先写进 user_memories 的行，每项 {kind, content}。
    #
    # 为什么要预置而不是让真实抽取产生：抽取是 chat_router 里的异步触发，
    # 这套评估直接驱动 chat_service，那条路根本不会跑。就算把它接进来，
    # 抽取要不要把某句话记成记忆本身由辅助模型决定——注入防线的对照组会
    # 因此每次形状都不一样。预置把变量固定成一条：**这行记忆已经在库里了，
    # 模型会不会照它说的做。** 这正好是 fence + 声明这层防线负责的事。
    seed_memories: list[dict[str, str]] = field(default_factory=list)


def load_tasks(limit: int | None = None, path: str | None = None) -> list[AgentTask]:
    tasks: list[AgentTask] = []
    attachment = agent_stubs.ensure_attachment_fixture()
    with open(path or DATASET_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            raw = json.loads(line)
            turns = [
                TurnSpec(
                    # 附件路径由夹具决定，数据集里写占位符——把绝对路径写死在
                    # 数据集里，换台机器就解析不到了
                    question=str(item["question"]).replace("{attachment}", attachment),
                    expect_tools=list(item.get("expect_tools") or []),
                    allow_tools=list(item.get("allow_tools") or []),
                    forbid_tools=list(item.get("forbid_tools") or []),
                    expect_order=bool(item.get("expect_order", False)),
                    must_include=list(item.get("must_include") or []),
                    must_avoid=list(item.get("must_avoid") or []),
                    min_rounds=int(item.get("min_rounds", 1)),
                )
                for item in raw["turns"]
            ]
            tasks.append(
                AgentTask(
                    id=raw["id"],
                    probe=raw.get("probe", "general"),
                    rubric=raw.get("rubric", ""),
                    turns=turns,
                    use_rag=bool(raw.get("use_rag", True)),
                    stub_mode=raw.get("stub_mode", "ok"),
                    title=raw.get("title", ""),
                    seed_memories=[
                        {
                            "kind": str(item.get("kind") or "fact"),
                            "content": str(item["content"]),
                        }
                        for item in (raw.get("seed_memories") or [])
                    ],
                )
            )
    return tasks[:limit] if limit else tasks


@dataclass(slots=True)
class TurnOutcome:
    question: str
    answer: str
    # 模型自主发起的调用（round >= 1）。预检索另计。
    calls: list[dict[str, Any]]
    prefetch_calls: int
    rounds: int
    tool_recall: float
    tool_precision: float | None
    forbidden_hits: int
    order_ok: bool | None
    round_efficiency: float | None
    repeated_calls: int
    # 被 RepeatGuard 拦下、没有真正执行的次数。与 repeated_calls 分开看:
    # 后者量的是"模型重复了几次"(检测关掉时那笔浪费的基线),这里量的是
    # "拦住了几次"。两个数一起才说得清检测有没有生效、以及它拦的是不是同一批。
    repeated_blocked: int
    keyword_coverage: float
    avoid_hits: int
    guardrail_hits: int
    unavailable_calls: int
    invalid_calls: int
    errors: list[str]
    prompt_tokens: int
    completion_tokens: int
    cost: float | None
    currency: str | None
    latency_ms: int


@dataclass(slots=True)
class TaskResult:
    task: AgentTask
    turns: list[TurnOutcome]
    verdict: TaskVerdict
    # Agent 真的写进知识库的文档名。写操作是唯一会改变工作区状态的工具，
    # 所以它做了什么必须逐条留痕，而不是只体现在一个分数里。
    written_documents: list[str]
    stub_queries: list[str]
    stub_misses: list[str]
    evidence_steps: int


def preflight() -> list[str]:
    """跑之前先查那些「会让整批数字变成垃圾」的前置条件。

    宁可在第一次模型调用之前退出，也不要跑完二十分钟才发现指标全是 0——
    尤其是缺表这一类：``tool_history.record`` 出错时只记一条 warning 然后咽掉
    （那是对线上请求正确的取舍），于是评估会安静地得到一份"模型完全不记事"的报告。
    """
    problems: list[str] = []
    try:
        tables = set(inspect(engine).get_table_names())
    except Exception as exc:
        return [f"无法连接数据库：{type(exc).__name__}"]

    if "message_tool_steps" not in tables:
        problems.append(
            "缺少 message_tool_steps 表——先执行 alembic upgrade head。"
            "少了它，工具轨迹会被静默丢弃，memory 探针会全军覆没，"
            "而失败看起来像是模型不记事。"
        )
    if "trace_spans" not in tables:
        problems.append("缺少 trace_spans 表，token 与成本列会全部为 0。")
    if "user_memories" not in tables:
        # 注入路径(chat_service 里 MEMORY_ENABLED 那段)没有 try/except，
        # 缺表会让**每一轮**都抛，不只是 injection_memory 这一个任务。
        problems.append(
            "缺少 user_memories 表——先执行 alembic upgrade head。"
            "记忆注入没有兜底，缺表会让每一轮都直接失败。"
        )
    if not os.path.isdir(CORPUS_DIR):
        problems.append(f"语料目录不存在：{CORPUS_DIR}")
    if not settings.LLM_API_KEY:
        problems.append("LLM_API_KEY 为空，模型调用会全部失败。")
    return problems


def _span_totals(db: Any, message_id: str) -> tuple[int, int, float | None, str | None]:
    """从 ``trace_spans`` 反查这一轮的 token 与成本。

    先 commit 再查：埋点是另一条连接写进去的，而 MySQL 默认的 REPEATABLE READ
    会让当前事务一直看着它开始时的快照。不 commit 就可能一行也查不到，
    然后报告里所有成本都是 0 —— 一个非常难查的"零"。
    """
    db.commit()
    rows = db.query(TraceSpan).filter(TraceSpan.message_id == message_id).all()
    prompt = sum(row.prompt_tokens or 0 for row in rows)
    completion = sum(row.completion_tokens or 0 for row in rows)

    by_currency: dict[str, float] = {}
    for row in rows:
        if row.cost is None or not row.currency:
            continue
        by_currency[row.currency] = by_currency.get(row.currency, 0.0) + float(row.cost)
    if not by_currency:
        return prompt, completion, None, None
    currency = max(by_currency, key=lambda key: by_currency[key])
    return prompt, completion, by_currency[currency], currency


def _evidence(db: Any, chat_id: str) -> tuple[str, int]:
    """把落库的工具轨迹渲染成给裁判看的证据。

    读的是 ``message_tool_steps`` 而不是内存里攒的事件：事件只带工具名与状态，
    结果正文只在那张表里。顺带这也验证了持久化链路真的写成了——
    裁判拿不到证据时给出的低 grounded 分，本身就是一个有效信号。
    """
    try:
        rows = (
            db.query(MessageToolStep)
            .filter(MessageToolStep.chat_id == chat_id)
            .order_by(
                MessageToolStep.created_at.asc(),
                MessageToolStep.round_index.asc(),
                MessageToolStep.call_index.asc(),
            )
            .all()
        )
    except Exception as exc:
        logger.warning("tool steps unavailable: %s", type(exc).__name__)
        return "", 0

    blocks: list[str] = []
    for row in rows:
        where = (
            "预检索"
            if row.round_index == PREFETCH_ROUND
            else f"第 {row.round_index} 轮"
        )
        # 带上执行者。不标的话委派模式下裁判看到的是一串扁平的工具调用，
        # 分不清"researcher 查到了这个"和"主代理自己查到了这个"——而这正是
        # 判断委派有没有起作用要看的第一件事。单代理模式下 agent_role 为空，
        # 渲染结果与此前逐字相同。
        who = f" [{row.agent_role} 子代理]" if row.agent_role else ""
        blocks.append(
            f"[{where}]{who} {row.tool_name}({row.arguments or '{}'}) → {row.status}\n"
            f"{row.result_content or '（无内容）'}"
        )
    return "\n\n".join(blocks), len(rows)


async def _drive_turn(
    service: ChatService,
    db: Any,
    chat_id: str,
    spec: TurnSpec,
    *,
    use_rag: bool,
    model: str,
) -> TurnOutcome:
    """跑一轮，按事件流还原模型这一轮做了什么。

    这里刻意走 ``save_message`` -> ``stream_ai_response`` -> ``save_message``
    这条和路由完全相同的顺序：用户消息必须先落库，因为下一轮的历史是从库里读的，
    而 ``message_id`` 既是轨迹归属的键，也是这一轮埋点的键。
    """
    user_message_id = str(uuid.uuid4())
    await service.save_message(db, chat_id, "user", spec.question, model, user_message_id)

    answer_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    statuses: list[str] = []
    prefetch_calls = 0
    guardrail_hits = 0
    errors: list[str] = []
    started = time.perf_counter()

    async for event in service.stream_ai_response(
        db,
        EVAL_USER_ID,
        chat_id,
        spec.question,
        model=model,
        use_rag=use_rag,
        message_id=user_message_id,
        # 温度 0：见模块说明第 4 条
        temperature=0.0,
        max_tokens=1024,
        top_p=1.0,
    ):
        kind = event.get("type")
        if kind == "message_delta":
            answer_parts.append(event.get("content") or "")
        elif kind == "tool_start":
            round_index = int(event.get("round") or 0)
            if round_index == PREFETCH_ROUND:
                prefetch_calls += 1
            else:
                calls.append(
                    {
                        "tool": event.get("tool", ""),
                        "round": round_index,
                        "input": event.get("input") or {},
                    }
                )
        elif kind == "agent_step" and event.get("phase") == "tool_start":
            # 子代理的工具调用。不记的话委派模式下的指标会谎报:supervisor 模式里
            # researcher 真的检索了,但 expect_tools=["search_knowledge_base"] 会
            # 算成 recall=0——那是"指标看不到",不是"模型没做"。
            #
            # 对现有变体是空操作:它们的 AGENT_DELEGATION_MODE 是 off,不会有
            # agent_step 事件。所以这条不会改动任何已跑出来的基线数字。
            #
            # 记成普通调用而不是单独一列,是因为任务集问的是"这一轮该不该查知识库",
            # 而不是"该由谁去查"。谁去查属于委派策略,由 delegate 自己的出现次数
            # 和轮次成本体现。
            calls.append(
                {
                    "tool": event.get("tool", ""),
                    "round": int(event.get("round") or 0),
                    "input": event.get("input") or {},
                    "agent": str(event.get("agent") or ""),
                }
            )
        elif kind == "tool_result":
            if int(event.get("round") or 0) != PREFETCH_ROUND:
                statuses.append(str(event.get("status") or ""))
        elif kind == "agent_step" and event.get("phase") == "tool_result":
            statuses.append(str(event.get("status") or ""))
        elif kind == "guardrail":
            guardrail_hits += 1
        elif kind == "error":
            errors.append(str(event.get("error") or "unknown"))
        elif kind == "cache_hit":
            # 变体基线把语义缓存关掉了；真出现说明配置串了，这一批数字不能用
            errors.append("semantic_cache_hit")

    latency_ms = int((time.perf_counter() - started) * 1000)
    answer = "".join(answer_parts)
    if answer.strip() and not errors:
        await service.save_message(db, chat_id, "assistant", answer, model)

    names = [call["tool"] for call in calls]
    # 轮次 = 最后一个有工具调用的轮次 + 1（那一轮用来作答）。没调工具就是 1 轮。
    # 事件流里没有"作答轮"的标记，这个推算和 turn.set(rounds=...) 是一致的。
    last_tool_round = max((call["round"] for call in calls), default=0)
    rounds = last_tool_round + 1 if last_tool_round else 1
    prompt_tokens, completion_tokens, cost, currency = _span_totals(db, user_message_id)

    return TurnOutcome(
        question=spec.question,
        answer=answer,
        calls=calls,
        prefetch_calls=prefetch_calls,
        rounds=rounds,
        tool_recall=agent_metrics.tool_recall(names, spec.expect_tools),
        tool_precision=agent_metrics.tool_precision(
            names, spec.expect_tools + spec.allow_tools
        ),
        forbidden_hits=agent_metrics.forbidden_hits(names, spec.forbid_tools),
        order_ok=(
            agent_metrics.order_respected(names, spec.expect_tools)
            if spec.expect_order
            else None
        ),
        round_efficiency=agent_metrics.round_efficiency(rounds, spec.min_rounds),
        repeated_calls=agent_metrics.repeated_calls(
            [agent_metrics.canonical_call(call["tool"], call["input"]) for call in calls]
        ),
        keyword_coverage=metrics.keyword_coverage(
            # 先去掉千分位分隔符：模型写「4,740」和写「4740」是同一个答案，
            # 而关键词命中是纯子串匹配，不去掉就会把答对的样本判成没命中。
            answer.replace(",", "").replace("，", ""),
            spec.must_include,
        ),
        avoid_hits=sum(
            1 for phrase in spec.must_avoid if phrase.lower() in answer.lower()
        ),
        guardrail_hits=guardrail_hits,
        unavailable_calls=sum(1 for status in statuses if status == "unavailable"),
        invalid_calls=sum(1 for status in statuses if status == "invalid_arguments"),
        repeated_blocked=sum(1 for status in statuses if status == "repeated"),
        errors=errors,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        currency=currency,
        latency_ms=latency_ms,
    )


def _purge_memories(db: Any) -> int:
    """清掉伪用户名下所有长期记忆，返回删除条数。

    记忆是**按用户**存的，不按会话——所以它跨任务、跨变体存活，和
    ``_sweep_written_documents`` 处理的是同一个问题：评估的每次运行都必须从
    同一个已知状态出发。一次中断的运行留下的一行记忆，会被后面每个任务、
    每个变体读到，而报告上看不出任何异常。

    因此这里在**播种之前也要清一遍**，不能只在结束时清。
    """
    rows = db.query(UserMemory).filter(UserMemory.user_id == EVAL_USER_ID).all()
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
    return len(rows)


def _seed_memories(db: Any, items: list[dict[str, str]], chat_id: str) -> None:
    """把任务声明的记忆行写进库。

    ``created_at`` 逐条递增：注入是按 created_at 倒序取的（"新说的压过旧说的"），
    全部写成同一时刻的话顺序就由数据库返回顺序决定，模型看到的块次序会在不同
    机器上不一样。差 1 秒足够定序，也不会撞上 MEMORY_INJECT_LIMIT。
    """
    base = naive_now()
    for offset, item in enumerate(items):
        db.add(
            UserMemory(
                user_id=EVAL_USER_ID,
                kind=item["kind"],
                content=item["content"],
                chat_id=chat_id,
                created_at=base + timedelta(seconds=offset),
            )
        )
    db.commit()


def _sweep_written_documents(db: Any) -> list[str]:
    """把 Agent 写进知识库的文档收走，返回它们的名字。

    不收的话下一个任务、以及下一个变体，都会检索到上一个任务留下的笔记，
    对照立刻不成立。这是 ``ensure_corpus`` 用分块指纹解决的同一个问题在写侧的版本：
    评估的每一次运行都必须从同一个已知状态出发。

    能这么干净地一网打尽，靠的是写入时强制加了 ``WRITE_NAME_PREFIX``——
    当初那个前缀是为了让 Agent 写的文档在引用列表里一眼可辨，
    这里顺带成了"只删它写的、不碰语料"的判据。
    """
    rows = (
        db.query(Document)
        .filter(
            Document.workspace_id == _eval_workspace_id(db),
            Document.name.like(f"{WRITE_NAME_PREFIX}%"),
        )
        .all()
    )
    names = [row.name for row in rows]
    for row in rows:
        db.delete(row)
    if names:
        db.commit()
        invalidate_scope_indexes(_eval_workspace_id(db))
    return names


async def run_task(
    service: ChatService, judge: TaskJudge, task: AgentTask, model: str
) -> TaskResult:
    """跑完一个任务。每个任务一个一次性对话，跑完连对话带轨迹一起删。

    共用一个对话会让任务之间互相污染（上一轮的轨迹会被回灌到下一个任务），
    而这套评估最想量的恰恰就是轨迹回灌——那样测出来的数字会好得莫名其妙。
    """
    db = SessionLocal()
    chat = None
    try:
        ensure_eval_user(db)
        chat = await service.create_chat(db, user_id=EVAL_USER_ID, title=f"eval:{task.id}")
        # 每个任务都先清空记忆,不只是声明了 seed_memories 的那些:留下的行会被
        # 后面每个任务读到,而绝大多数任务并不预期库里有记忆。
        stale = _purge_memories(db)
        if stale:
            logger.warning(
                "%s: 清掉了 %s 条残留记忆——上一次运行中断过", task.id, stale
            )
        if task.seed_memories:
            _seed_memories(db, task.seed_memories, chat.id)
        outcomes: list[TurnOutcome] = []
        with agent_stubs.stub_web_search(task.stub_mode) as stub:
            for spec in task.turns:
                outcomes.append(
                    await _drive_turn(
                        service,
                        db,
                        chat.id,
                        spec,
                        use_rag=task.use_rag,
                        model=model,
                    )
                )
            stub_queries = list(stub.queries)
            stub_misses = list(stub.misses)

        # 证据必须在删对话之前取
        evidence, evidence_steps = _evidence(db, chat.id)
        written = _sweep_written_documents(db)

        if outcomes:
            transcript = "\n".join(
                f"第 {index} 轮提问：{outcome.question}"
                for index, outcome in enumerate(outcomes, start=1)
            )
            # 只判最后一轮的回答：rubric 就是照着"最终回答要体现什么"写的，
            # 中间轮次由确定性指标负责，这样裁判开销固定为每任务一次。
            verdict = await judge.judge(
                question=transcript,
                answer=outcomes[-1].answer,
                evidence=evidence,
                rubric=task.rubric,
            )
        else:
            verdict = TaskVerdict(reason="任务没有轮次", failed=True)

        return TaskResult(
            task=task,
            turns=outcomes,
            verdict=verdict,
            written_documents=written,
            stub_queries=stub_queries,
            stub_misses=stub_misses,
            evidence_steps=evidence_steps,
        )
    finally:
        # 记忆先清:它挂在用户身上,不会随对话一起删。放在 finally 里是因为
        # 任务中途抛异常时留下的行会污染后面所有任务。
        try:
            _purge_memories(db)
        except Exception as exc:
            logger.warning("memory cleanup failed for %s: %s", task.id, type(exc).__name__)
        if chat is not None:
            try:
                await service.delete_chat(db, chat.id)
            except Exception as exc:  # 清理失败不该让整批评估中断
                logger.warning("cleanup failed for %s: %s", task.id, type(exc).__name__)
        db.close()


def summarize(variant: AgentVariant, results: list[TaskResult]) -> dict[str, Any]:
    """把逐任务结果汇总成一行。

    每个指标只在"它有定义"的那批样本上求均值：没标必需工具的轮次不进工具召回，
    没标 must_avoid 的不进抗注入率，裁判解析失败的不进成功率。用 0 填空会把
    "这里没有可判定的标的"和"这里判定为失败"混成同一个数字。
    """
    pairs = [
        (spec, outcome)
        for result in results
        for spec, outcome in zip(result.task.turns, result.turns)
    ]
    graded = [result for result in results if not result.verdict.failed]

    recall_values = [out.tool_recall for spec, out in pairs if spec.expect_tools]
    precision_values = [
        out.tool_precision for _spec, out in pairs if out.tool_precision is not None
    ]
    order_values = [
        1.0 if out.order_ok else 0.0 for _spec, out in pairs if out.order_ok is not None
    ]
    efficiency_values = [
        out.round_efficiency
        for _spec, out in pairs
        if out.round_efficiency is not None
    ]
    keyword_values = [out.keyword_coverage for spec, out in pairs if spec.must_include]
    injection_turns = [out for spec, out in pairs if spec.must_avoid]

    summary: dict[str, Any] = {
        "variant": variant.name,
        "description": variant.description,
        "tasks": len(results),
        "turns": len(pairs),
        "taskSuccess": metrics.mean(
            [g.verdict.success for g in graded if g.verdict.success is not None]
        ),
        "grounded": metrics.mean(
            [g.verdict.grounded for g in graded if g.verdict.grounded is not None]
        ),
        # 声称调过工具但轨迹里没有——Agent 特有的失效模式，单独计数不进均值
        "fabricatedToolOutput": sum(
            1 for g in graded if g.verdict.fabricated_tool_output
        ),
        "judgeFailures": sum(1 for result in results if result.verdict.failed),
        "toolRecall": metrics.mean(recall_values),
        "toolPrecision": metrics.mean(precision_values),
        "toolOrderRate": metrics.mean(order_values),
        "roundEfficiency": metrics.mean(efficiency_values),
        "avgRounds": metrics.mean([float(out.rounds) for _spec, out in pairs]),
        "repeatedCalls": sum(out.repeated_calls for _spec, out in pairs),
        "repeatedBlocked": sum(out.repeated_blocked for _spec, out in pairs),
        "keywordCoverage": metrics.mean(keyword_values),
        # 硬要求：任何一次都算违规，所以报总数而不是比率
        "forbiddenCalls": sum(out.forbidden_hits for _spec, out in pairs),
        "prefetchCalls": sum(out.prefetch_calls for _spec, out in pairs),
        "modelToolCalls": sum(len(out.calls) for _spec, out in pairs),
        "unavailableCalls": sum(out.unavailable_calls for _spec, out in pairs),
        "invalidCalls": sum(out.invalid_calls for _spec, out in pairs),
        "guardrailHits": sum(out.guardrail_hits for _spec, out in pairs),
        "turnErrors": sum(1 for _spec, out in pairs if out.errors),
        "writtenDocuments": sum(len(result.written_documents) for result in results),
        "stubMisses": sum(len(result.stub_misses) for result in results),
        "promptTokens": sum(out.prompt_tokens for _spec, out in pairs),
        "completionTokens": sum(out.completion_tokens for _spec, out in pairs),
        "avgLatencyMs": metrics.mean([float(out.latency_ms) for _spec, out in pairs]),
    }

    summary["injectionCases"] = len(injection_turns)
    summary["injectionResistRate"] = (
        metrics.mean([1.0 if out.avoid_hits == 0 else 0.0 for out in injection_turns])
        if injection_turns
        else None
    )

    costs = [out.cost for _spec, out in pairs if out.cost is not None]
    summary["cost"] = sum(costs) if costs else None
    summary["currency"] = next(
        (out.currency for _spec, out in pairs if out.currency), None
    )

    by_probe: dict[str, list[float]] = {}
    for result in graded:
        if result.verdict.success is None:
            continue
        by_probe.setdefault(result.task.probe, []).append(result.verdict.success)
    summary["successByProbe"] = {
        probe: metrics.mean(values) for probe, values in sorted(by_probe.items())
    }
    return summary


async def run_variant(
    variant: AgentVariant, tasks: list[AgentTask]
) -> tuple[dict[str, Any], list[TaskResult]]:
    """套用变体配置跑完一轮，结束后恢复原配置。"""
    original = {key: getattr(settings, key) for key in variant.overrides}
    for key, value in variant.overrides.items():
        setattr(settings, key, value)

    try:
        adapter = OpenAICompatibleAdapter()
        service = ChatService(adapter)
        judge = TaskJudge(adapter)
        knowledge = KnowledgeService()

        # 变体可能改了 PROMPT_CHAT_SYSTEM_VERSION，所以在套用配置之后才解析模板。
        # 版本不存在时这里直接抛，早于任何一次模型调用。
        system_prompt = prompt_library.get("chat_system_rag")
        chunks, reindexed = await ensure_corpus(knowledge)
        logger.info(
            "[%s] corpus ready: %s chunks%s | prompt=%s | tools=%s",
            variant.name,
            chunks,
            " (reindexed)" if reindexed else "",
            system_prompt.ref,
            ",".join(workspace_enabled_names()),
        )

        results: list[TaskResult] = []
        for index, task in enumerate(tasks, start=1):
            result = await run_task(service, judge, task, settings.LLM_MODEL)
            results.append(result)
            logger.info(
                "[%s] %s/%s %s success=%s rounds=%s calls=%s",
                variant.name,
                index,
                len(tasks),
                task.id,
                result.verdict.success,
                [turn.rounds for turn in result.turns],
                [call["tool"] for turn in result.turns for call in turn.calls],
            )

        summary = summarize(variant, results)
        summary["corpusChunks"] = chunks
        summary["systemPrompt"] = system_prompt.version
        return summary, results
    finally:
        for key, value in original.items():
            setattr(settings, key, value)


def _turn_detail(spec: TurnSpec, outcome: TurnOutcome) -> dict[str, Any]:
    return {
        "question": outcome.question,
        "answer": outcome.answer,
        "expectTools": spec.expect_tools,
        "forbidTools": spec.forbid_tools,
        "calls": outcome.calls,
        "prefetchCalls": outcome.prefetch_calls,
        "rounds": outcome.rounds,
        "minRounds": spec.min_rounds,
        "toolRecall": outcome.tool_recall,
        "toolPrecision": outcome.tool_precision,
        "forbiddenHits": outcome.forbidden_hits,
        "orderOk": outcome.order_ok,
        "roundEfficiency": outcome.round_efficiency,
        "repeatedCalls": outcome.repeated_calls,
        "repeatedBlocked": outcome.repeated_blocked,
        "keywordCoverage": outcome.keyword_coverage,
        "avoidHits": outcome.avoid_hits,
        "guardrailHits": outcome.guardrail_hits,
        "unavailableCalls": outcome.unavailable_calls,
        "invalidCalls": outcome.invalid_calls,
        "errors": outcome.errors,
        "promptTokens": outcome.prompt_tokens,
        "completionTokens": outcome.completion_tokens,
        "latencyMs": outcome.latency_ms,
    }


async def run(
    variants: list[AgentVariant], tasks: list[AgentTask]
) -> dict[str, Any]:
    # 评估依赖埋点来算成本与延迟，强制打开
    settings.TELEMETRY_ENABLED = True

    summaries: list[dict[str, Any]] = []
    details: dict[str, list[dict[str, Any]]] = {}
    for variant in variants:
        summary, results = await run_variant(variant, tasks)
        summaries.append(summary)
        details[variant.name] = [
            {
                "id": result.task.id,
                "probe": result.task.probe,
                "title": result.task.title,
                "rubric": result.task.rubric,
                "useRag": result.task.use_rag,
                "stubMode": result.task.stub_mode,
                "success": result.verdict.success,
                "grounded": result.verdict.grounded,
                "fabricatedToolOutput": result.verdict.fabricated_tool_output,
                "judgeReason": result.verdict.reason,
                "judgeFailed": result.verdict.failed,
                "evidenceSteps": result.evidence_steps,
                "writtenDocuments": result.written_documents,
                # 实际搜索词原样留下：罐头结果是按关键词命中的，
                # 命中不了的查询会以 stubMisses 出现，靠这一列去调数据集
                "stubQueries": result.stub_queries,
                "stubMisses": result.stub_misses,
                # 预置的记忆原样留下：这个任务失败时第一件要确认的事就是
                # "模型当时到底看到了什么"，而它不在对话里、也不在工具轨迹里
                "seedMemories": result.task.seed_memories,
                "turns": [
                    _turn_detail(spec, outcome)
                    for spec, outcome in zip(result.task.turns, result.turns)
                ],
            }
            for result in results
        ]
    return {"summaries": summaries, "details": details}
