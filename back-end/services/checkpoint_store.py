"""快照与执行记录的持久化。

这一层刻意做得很薄——它只有"存一份、取最新的一份、按 seq 取某一份、清理"这几个
动作。手写而不是直接用 LangGraph 的 ``BaseCheckpointSaver``，是因为这四个动作的
形状本身就是要搞清楚的东西：

- **``thread_id`` 的粒度到底是什么。** LangGraph 里一个 thread 通常是一个会话；
  这里选的是**一个回合**（``run_id``）。选会话的话，同一个对话里前后两次回答的
  快照会互相覆盖，而"恢复"要的是回到某一次回答的中途，不是回到上一次回答。
- **为什么要留历史 seq 而不是原地覆盖。** 覆盖之后只能"继续"，不能"重放"。
  评估复现要的是后者。
- **pending writes 为什么单独存。** 见 ``agent_state.make_write``：一批工具里
  执行完三个卡在第四个，重跑整批就是让前三次的钱白花第二遍。这里把它们
  放在同一份快照的 ``writes`` 字段里而不是另开一张表——它们和快照是同生同死的，
  分表只会多一次 join 和一致性问题。

失败处理与 ``tool_history`` 同一套：**任何持久化失败只记日志，不打断回答**。
快照存不下的后果是"这次不能恢复"，而不是"这次答不出来"。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from config import settings
from models import AgentCheckpoint, AgentRun
from services.agent_state import TurnState
from services.clock import naive_now

logger = logging.getLogger("checkpoint_store")


def enabled() -> bool:
    return bool(settings.AGENT_CHECKPOINT_ENABLED)


# ---------- agent_runs ----------


def start_run(
    db: Session,
    *,
    run_id: str,
    chat_id: str,
    user_id: str,
    message_id: str | None,
    model: str | None,
    prompt_ref: str | None,
    trace_id: str | None = None,
    parent_run_id: str | None = None,
    agent_role: str | None = None,
) -> None:
    """开一行执行记录。失败只记日志。"""
    if not enabled():
        return
    now = naive_now()
    try:
        db.add(
            AgentRun(
                id=run_id,
                chat_id=chat_id,
                user_id=user_id,
                message_id=message_id,
                parent_run_id=parent_run_id,
                agent_role=agent_role[:40] if agent_role else None,
                status="running",
                model=model[:100] if model else None,
                prompt_ref=prompt_ref[:120] if prompt_ref else None,
                trace_id=trace_id[:32] if trace_id else None,
                started_at=now,
                updated_at=now,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("start_run failed for %s", run_id, exc_info=True)


def update_run(
    db: Session,
    run_id: str,
    *,
    status: str | None = None,
    rounds: int | None = None,
    delegations: int | None = None,
    bump_interrupts: bool = False,
    error_type: str | None = None,
    finished: bool = False,
) -> None:
    """推进执行记录。只改传进来的字段。"""
    if not enabled():
        return
    try:
        run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if run is None:
            return
        if status is not None:
            run.status = status
        if rounds is not None:
            run.rounds = rounds
        if delegations is not None:
            run.delegations = delegations
        if bump_interrupts:
            run.interrupts = (run.interrupts or 0) + 1
        if error_type is not None:
            run.error_type = error_type[:80]
        now = naive_now()
        run.updated_at = now
        if finished:
            run.finished_at = now
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("update_run failed for %s", run_id, exc_info=True)


def get_run(db: Session, run_id: str) -> AgentRun | None:
    return db.query(AgentRun).filter(AgentRun.id == run_id).first()


def list_pending(db: Session, user_id: str, limit: int = 20) -> list[AgentRun]:
    """这个用户所有等待审批的执行。

    刷新页面之后前端靠它把审批卡片找回来——中断活在数据库里，不活在那条
    已经断掉的 SSE 连接里，这正是可恢复执行与"一个长连接等着用户点"的区别。
    """
    return (
        db.query(AgentRun)
        .filter(AgentRun.user_id == user_id, AgentRun.status == "waiting_approval")
        .order_by(AgentRun.updated_at.desc())
        .limit(limit)
        .all()
    )


def child_runs(db: Session, parent_run_id: str) -> list[AgentRun]:
    return (
        db.query(AgentRun)
        .filter(AgentRun.parent_run_id == parent_run_id)
        .order_by(AgentRun.started_at.asc())
        .all()
    )


def discard_chat(db: Session, chat_id: str) -> None:
    """删对话时连带清掉执行记录与快照。

    快照里存着对话正文，用户删了对话却留下一份完整副本是说不过去的。
    """
    try:
        runs = db.query(AgentRun).filter(AgentRun.chat_id == chat_id).all()
        run_ids = [run.id for run in runs]
        if run_ids:
            db.query(AgentCheckpoint).filter(
                AgentCheckpoint.run_id.in_(run_ids)
            ).delete(synchronize_session=False)
        db.query(AgentRun).filter(AgentRun.chat_id == chat_id).delete(
            synchronize_session=False
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("discard_chat failed for %s", chat_id, exc_info=True)


# ---------- agent_checkpoints ----------


def put(
    db: Session,
    state: TurnState,
    *,
    interrupt: dict[str, Any] | None = None,
) -> int | None:
    """写一份快照，返回它的 seq。

    seq 由"当前最大 + 1"算出而不是用自增列：它要能在一次事务里确定，
    审批链路上"写快照 -> 把 run 置为等待 -> 返回 seq 给前端"必须拿到同一个数。

    并发写同一个 run 的可能性被 ``uq_agent_checkpoints_run_seq`` 挡住——同一个
    run 本来也只该有一个驱动者，撞上唯一约束说明有两个请求在同时跑同一次执行，
    那时候失败比静默双写要好。
    """
    if not enabled():
        return None
    try:
        current = (
            db.query(AgentCheckpoint.seq)
            .filter(AgentCheckpoint.run_id == state.run_id)
            .order_by(AgentCheckpoint.seq.desc())
            .first()
        )
        seq = (current[0] if current else -1) + 1
        db.add(
            AgentCheckpoint(
                run_id=state.run_id,
                seq=seq,
                phase=state.phase,
                round_index=state.round_index,
                state=state.to_json(),
                interrupt=(
                    json.dumps(interrupt, ensure_ascii=False) if interrupt else None
                ),
                created_at=naive_now(),
            )
        )
        db.commit()
        _prune(db, state.run_id)
        return seq
    except Exception:
        db.rollback()
        logger.warning("checkpoint put failed for %s", state.run_id, exc_info=True)
        return None


def latest(db: Session, run_id: str) -> TurnState | None:
    """取最新快照。反序列化失败按"没有快照"处理。

    宁可让恢复失败，也不要拿一份半截的状态去跑——那会让模型在一个自己没说过的
    上下文里继续，比直接告诉用户"这次恢复不了"要难查得多。
    """
    row = (
        db.query(AgentCheckpoint)
        .filter(AgentCheckpoint.run_id == run_id)
        .order_by(AgentCheckpoint.seq.desc())
        .first()
    )
    if row is None:
        return None
    try:
        return TurnState.from_json(row.state)
    except Exception:
        logger.warning("checkpoint decode failed for %s", run_id, exc_info=True)
        return None


def at_seq(db: Session, run_id: str, seq: int) -> TurnState | None:
    """取某一份历史快照。重放（回到第 N 轮再跑一次）走这条路。"""
    row = (
        db.query(AgentCheckpoint)
        .filter(AgentCheckpoint.run_id == run_id, AgentCheckpoint.seq == seq)
        .first()
    )
    if row is None:
        return None
    try:
        return TurnState.from_json(row.state)
    except Exception:
        logger.warning("checkpoint decode failed for %s@%s", run_id, seq, exc_info=True)
        return None


def history(db: Session, run_id: str) -> list[dict[str, Any]]:
    """快照目录（不含状态正文）。给调试面板列"这次执行有哪些可回到的点"。"""
    rows = (
        db.query(AgentCheckpoint)
        .filter(AgentCheckpoint.run_id == run_id)
        .order_by(AgentCheckpoint.seq.asc())
        .all()
    )
    return [
        {
            "seq": row.seq,
            "phase": row.phase,
            "round": row.round_index,
            "bytes": len(row.state or ""),
            "interrupt": json.loads(row.interrupt) if row.interrupt else None,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def _prune(db: Session, run_id: str) -> None:
    """只保留最近 N 份快照。

    一份快照就是整个 messages 列表，六轮下来能有几十 KB。不清理的话这张表会
    比 messages 本身还大，而第 1 轮的快照在第 6 轮已经没人会回去了。
    保留数由 ``AGENT_CHECKPOINT_KEEP`` 决定，0 表示不清理。
    """
    keep = max(0, settings.AGENT_CHECKPOINT_KEEP)
    if keep <= 0:
        return
    try:
        rows = (
            db.query(AgentCheckpoint.id)
            .filter(AgentCheckpoint.run_id == run_id)
            .order_by(AgentCheckpoint.seq.desc())
            .offset(keep)
            .all()
        )
        stale = [row[0] for row in rows]
        if not stale:
            return
        db.query(AgentCheckpoint).filter(AgentCheckpoint.id.in_(stale)).delete(
            synchronize_session=False
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("checkpoint prune failed for %s", run_id, exc_info=True)


__all__ = [
    "at_seq",
    "child_runs",
    "discard_chat",
    "enabled",
    "get_run",
    "history",
    "latest",
    "list_pending",
    "put",
    "start_run",
    "update_run",
]
