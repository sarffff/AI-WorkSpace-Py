"""Agent 工具轨迹的持久化与跨回合回灌。

循环在一个回合内本来就是多轮的：工具结果按 ``role=tool`` 回灌进 ``messages``，
下一轮模型看得到。但回合结束时那个列表就被丢掉了，落库的只有最终回答的文本。
于是下一个回合里模型对自己上一回合读过什么一无所知——用户接着问「你刚看的
第 3 块里还写了什么」，它只能重新检索一遍，或者照着自己上次的措辞往回编。
这个模块把轨迹存下来，并在下一回合按预算回灌。

两个设计决定：

**回灌成一段 system 记录，而不是还原 role=tool 消息。** 还原需要连带伪造对应的
assistant tool_calls 消息，缺一个 tool_call_id 就是 400；GLM 的文本工具协议
根本没有 role=tool。语义上也不同：上一回合的结果是「我做过什么」，
不是「你刚要的东西在这」，摆成 role=tool 会让模型以为这轮已经调过工具了。

**存全量、回灌时压缩。** 摘要多长、要不要带引用、失败的步骤留不留，都是会反复
调的策略。只存摘要等于把当时的策略烙进数据，回头想换粒度已经没有原始内容了。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from sqlalchemy.orm import Session

from config import settings
from models import MessageToolStep
from services.clock import naive_now
from services.token_budget import get_token_counter

logger = logging.getLogger("tool_history")

# 标题里写明"此前的"不是啰嗦：模型一旦把这段当成本轮的工具结果，就会认为
# 检索已经做过，直接跳过这轮真正需要的那次调用。
_BLOCK_HEADER = "[本对话此前的工具执行记录，仅供参考，不是本轮的工具结果]"
_FOOTER_WITH_TOOLS = "需要更完整或更新的内容时，请重新调用相应工具。"
_FOOTER_NO_TOOLS = "本轮没有可用工具，只能把上面的记录当作已知信息使用。"

_STATUS_LABEL = {
    "ok": "成功",
    "invalid_arguments": "参数错误",
    "unavailable": "工具不可用",
    "error": "执行失败",
}

# 每步最多列几条引用。全列出来的话，光 document_id 就能吃掉大半个预算
_MAX_CITATIONS_PER_STEP = 3


def _dump(value: Any) -> str | None:
    """序列化成 JSON。存不下的东西宁可丢掉这一个字段，也不要让整步记录失败。"""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _load(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _condense(text: str, limit: int) -> str:
    """把结果压成单行摘要。

    折行与缩进对模型没有信息量，却照样按 token 计价。先把连续空白折成一个空格
    再截断，同样的预算能多留将近一倍的实际内容。
    """
    collapsed = " ".join((text or "").split())
    if limit <= 0 or not collapsed:
        return ""
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _format_arguments(raw: str | None) -> str:
    """把调用参数压成紧凑的一行。解析不出来就原样截断，不要因为一条脏数据
    炸掉整段回灌——轨迹是辅助信息，可用性优先于完整性。"""
    parsed = _load(raw)
    if parsed is None:
        return _condense(raw or "", 80)
    if not isinstance(parsed, dict):
        return _condense(str(parsed), 80)
    if not parsed:
        return ""
    return ", ".join(
        f"{key}={_condense(str(value), 60)}" for key, value in parsed.items()
    )


def _format_citations(raw: str | None) -> str:
    items = _load(raw)
    if not isinstance(items, list) or not items:
        return ""

    refs: list[str] = []
    for item in items[:_MAX_CITATIONS_PER_STEP]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("document_name") or "未知文档")
        chunk_index = item.get("chunk_index")
        ref = f"{name}#{chunk_index}" if chunk_index is not None else name
        document_id = str(item.get("document_id") or "")
        # 带上 document_id：模型要接着 read_document_chunk 就必须有它，
        # 否则得先花一整轮 list_knowledge_documents 把 id 找回来。
        refs.append(f"{ref}(document_id: {document_id})" if document_id else ref)

    if not refs:
        return ""
    more = len(items) - len(refs)
    return "、".join(refs) + (f"，另有 {more} 处" if more > 0 else "")


def _render_step(step: MessageToolStep, *, step_chars: int) -> str:
    where = "预检索" if step.round_index == 0 else f"第 {step.round_index} 轮"
    # 子代理执行的步骤要标出来:回灌时不标的话,模型会把 researcher 查到的东西
    # 当成自己查的,下个回合被追问细节时答不上来也不知道该重新委派。
    if step.agent_role:
        where += f" {step.agent_role}"
    status = _STATUS_LABEL.get(step.status, step.status)
    lines = [
        f"- {where} {step.tool_name}({_format_arguments(step.arguments)}) → {status}"
    ]

    citations = _format_citations(step.citations)
    if citations:
        lines.append(f"  引用：{citations}")

    summary = _condense(step.result_content or "", step_chars)
    if summary:
        # 摘要短于原文时要把原文长度说出来。这不是装饰:模型据此判断
        # "这里还有更多内容",需要细节时会重新调工具,而不是拿摘要硬答。
        if step.result_chars > len(summary):
            lines.append(f"  摘要(原文 {step.result_chars} 字)：{summary}")
        else:
            lines.append(f"  结果：{summary}")
    return "\n".join(lines)


def render_block(
    steps: list[MessageToolStep], *, tools_available: bool
) -> tuple[str, int]:
    """把轨迹压成一段文本，返回 (正文, 实际保留的步数)。

    超预算时从**最旧**的步骤开始丢：追问针对的几乎总是刚刚发生的那几步，
    而最早那次"列一下有哪些文档"到第三个回合已经没人关心了。
    """
    if not steps:
        return "", 0

    counter = get_token_counter(settings.TOKEN_COUNTER)
    budget = max(0, settings.TOOL_HISTORY_TOKEN_BUDGET)
    footer = _FOOTER_WITH_TOOLS if tools_available else _FOOTER_NO_TOOLS
    used = counter.count(_BLOCK_HEADER) + counter.count(footer)
    if used >= budget:
        return "", 0

    step_chars = max(0, settings.TOOL_HISTORY_STEP_CHARS)
    kept: list[str] = []
    for step in reversed(steps):
        rendered = _render_step(step, step_chars=step_chars)
        cost = counter.count(rendered)
        if used + cost > budget:
            break
        used += cost
        kept.append(rendered)

    if not kept:
        return "", 0
    kept.reverse()
    return "\n".join([_BLOCK_HEADER, *kept, footer]), len(kept)


def build_messages(
    db: Session,
    chat_id: str,
    *,
    exclude_message_id: str | None = None,
    tools_available: bool,
) -> tuple[list[dict[str, str]], int]:
    """给出可直接拼进请求的消息片段，以及实际回灌了几步。"""
    steps = load_recent(db, chat_id, exclude_message_id=exclude_message_id)
    block, kept = render_block(steps, tools_available=tools_available)
    if not block:
        return [], 0
    return [{"role": "system", "content": block}], kept


def record(
    db: Session,
    *,
    chat_id: str,
    message_id: str | None,
    round_index: int,
    call_index: int,
    tool_name: str,
    status: str,
    result: str,
    tool_call_id: str | None = None,
    arguments: Any = None,
    citations: list[dict[str, Any]] | None = None,
    agent_role: str | None = None,
) -> None:
    """记一步工具执行。

    逐步提交是有意的：流被用户掐断、模型中途报错时，已经做完的那几步应该留下来
    ——它们花过的检索成本是真的，下个回合没必要再付一次。

    任何失败只记日志。轨迹是辅助能力，不该让它把回答本身弄挂。
    """
    if not settings.TOOL_HISTORY_ENABLED:
        return

    body = result or ""
    store_limit = max(0, settings.TOOL_HISTORY_STORE_MAX_CHARS)
    try:
        db.add(
            MessageToolStep(
                chat_id=chat_id,
                message_id=message_id,
                round_index=round_index,
                call_index=call_index,
                tool_name=tool_name[:120],
                tool_call_id=str(tool_call_id)[:80] if tool_call_id else None,
                arguments=_dump(arguments),
                agent_role=agent_role[:40] if agent_role else None,
                status=status,
                result_content=body[:store_limit] if store_limit else None,
                # 截断前的真实长度。摘要里要靠它告诉模型"原文还有多少"
                result_chars=len(body),
                citations=_dump(citations) if citations else None,
                created_at=naive_now(),
            )
        )
        db.commit()
    except Exception as exc:
        logger.warning(
            "failed to record tool step %s: %s", tool_name, type(exc).__name__
        )
        try:
            db.rollback()
        except Exception:  # pragma: no cover - 回滚也失败时无事可做
            pass


def load_recent(
    db: Session, chat_id: str, *, exclude_message_id: str | None = None
) -> list[MessageToolStep]:
    """取回本对话最近的工具步骤，按执行顺序（旧到新）返回。

    读取量由 ``TOOL_HISTORY_FETCH_LIMIT`` 限制，留几条最终由 token 预算决定
    ——和对话历史的两段式裁剪是同一套路。
    """
    if not settings.TOOL_HISTORY_ENABLED:
        return []

    try:
        query = db.query(MessageToolStep).filter(MessageToolStep.chat_id == chat_id)
        if exclude_message_id:
            # 当前回合自己的步骤不回灌给自己：重新生成时会把上一次的半截轨迹
            # 当成"以前做过的事",模型据此跳过本该重做的调用。
            query = query.filter(MessageToolStep.message_id != exclude_message_id)
        steps = (
            query.order_by(
                MessageToolStep.created_at.desc(),
                # DATETIME 只到秒，同一回合里的几步时间戳会打平，
                # 再按轮次与调用序号定序才有稳定的回放顺序
                MessageToolStep.round_index.desc(),
                MessageToolStep.call_index.desc(),
            )
            .limit(max(1, settings.TOOL_HISTORY_FETCH_LIMIT))
            .all()
        )
    except Exception as exc:
        logger.warning("failed to load tool steps: %s", type(exc).__name__)
        return []

    steps.reverse()
    return steps


def discard(db: Session, chat_id: str, message_ids: Iterable[str]) -> int:
    """删掉指定回合的轨迹。

    「编辑并重新生成」会砍掉后续分支，那些回合的轨迹必须跟着走：留着的话重新
    生成时会把废弃分支的检索结果当成"以前做过的事"回灌进去，模型据此跳过本该
    重做的调用——答案变了，而没有任何迹象说明为什么。
    """
    ids = [message_id for message_id in message_ids if message_id]
    if not ids:
        return 0
    return (
        db.query(MessageToolStep)
        .filter(
            MessageToolStep.chat_id == chat_id,
            MessageToolStep.message_id.in_(ids),
        )
        .delete(synchronize_session=False)
    )


def discard_chat(db: Session, chat_id: str) -> int:
    """删对话时清掉轨迹。这张表故意没有外键（见 models 里的说明），
    级联删不到它，得自己来。"""
    return (
        db.query(MessageToolStep)
        .filter(MessageToolStep.chat_id == chat_id)
        .delete(synchronize_session=False)
    )


def serialize(step: MessageToolStep) -> dict[str, Any]:
    """给前端重放时间线用。

    刷新页面之后 SSE 里的 tool_start / tool_result 已经不存在了，这是唯一
    能把那条时间线找回来的地方。字段名与 SSE 事件保持一致，前端两条路径
    可以共用同一个渲染函数。
    """
    return {
        "id": step.id,
        "messageId": step.message_id,
        "round": step.round_index,
        "callIndex": step.call_index,
        "tool": step.tool_name,
        # None 表示主代理自己调的。前端据此把子代理的步骤缩进到 delegate 那一步下面
        "agentRole": step.agent_role,
        "status": step.status,
        "input": _load(step.arguments) or {},
        "citations": _load(step.citations) or [],
        "resultChars": step.result_chars,
        "resultPreview": _condense(
            step.result_content or "", max(0, settings.TOOL_HISTORY_STEP_CHARS)
        ),
        "createdAt": step.created_at.isoformat() if step.created_at else None,
    }
