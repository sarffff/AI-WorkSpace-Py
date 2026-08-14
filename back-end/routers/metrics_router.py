"""用量与 trace 查询接口。

埋点写进 trace_spans 之后，这里只做聚合与树形还原——两个真正想回答的问题：
「钱花在哪个环节」和「这次回答慢在哪一步」。

成本按币种分组返回而不是加成一个数：价目表允许不同模型用不同货币，
把 CNY 和 USD 相加是错的。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from auth import get_current_user
from config import settings
from database import get_db
from models import TraceSpan, User
from services.clock import naive_now
from services.pricing import price_table
from services.semantic_cache import semantic_cache

router = APIRouter(prefix="/metrics", tags=["用量与追踪"])


def _window_start(days: int) -> datetime:
    # 用应用时钟而非 UTC：trace_spans.started_at 存的是应用时区的墙上时间
    return naive_now() - timedelta(days=days)


def _as_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _grouped(
    db: Session, user_id: str, since: datetime, *columns
) -> list[dict[str, Any]]:
    """按给定列聚合调用次数、token、耗时与成本。"""
    rows = (
        db.query(
            *columns,
            TraceSpan.currency,
            func.count(TraceSpan.id).label("calls"),
            func.coalesce(func.sum(TraceSpan.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(TraceSpan.completion_tokens), 0).label(
                "completion_tokens"
            ),
            func.avg(TraceSpan.duration_ms).label("avg_ms"),
            func.sum(TraceSpan.duration_ms).label("total_ms"),
            func.sum(TraceSpan.cost).label("cost"),
            func.sum(case((TraceSpan.status != "ok", 1), else_=0)).label("failures"),
        )
        .filter(TraceSpan.user_id == user_id, TraceSpan.started_at >= since)
        .group_by(*columns, TraceSpan.currency)
        .order_by(func.sum(TraceSpan.duration_ms).desc())
        .all()
    )

    results: list[dict[str, Any]] = []
    for row in rows:
        mapping = row._mapping
        key_values = {column.key: mapping[column.key] for column in columns}
        results.append(
            {
                **key_values,
                "calls": int(mapping["calls"]),
                "promptTokens": int(mapping["prompt_tokens"] or 0),
                "completionTokens": int(mapping["completion_tokens"] or 0),
                "avgMs": _as_float(mapping["avg_ms"]),
                "totalMs": int(mapping["total_ms"] or 0),
                "cost": _as_float(mapping["cost"]),
                "currency": mapping["currency"],
                "failures": int(mapping["failures"] or 0),
            }
        )
    return results


@router.get("/usage")
async def get_usage(
    days: int = Query(default=0, ge=0, le=90),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """统计窗口内的调用量、token、成本与失败情况。"""
    window = days or settings.METRICS_DEFAULT_DAYS
    since = _window_start(window)

    totals_row = (
        db.query(
            func.count(TraceSpan.id).label("spans"),
            func.count(func.distinct(TraceSpan.trace_id)).label("turns"),
            func.coalesce(func.sum(TraceSpan.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(TraceSpan.completion_tokens), 0).label(
                "completion_tokens"
            ),
            func.sum(
                case((TraceSpan.token_source == "estimated", 1), else_=0)
            ).label("estimated_calls"),
            func.sum(case((TraceSpan.token_source.isnot(None), 1), else_=0)).label(
                "token_calls"
            ),
            func.sum(case((TraceSpan.status != "ok", 1), else_=0)).label("failures"),
        )
        .filter(TraceSpan.user_id == current_user.id, TraceSpan.started_at >= since)
        .one()
    )
    totals = totals_row._mapping

    cost_rows = (
        db.query(TraceSpan.currency, func.sum(TraceSpan.cost).label("amount"))
        .filter(
            TraceSpan.user_id == current_user.id,
            TraceSpan.started_at >= since,
            TraceSpan.cost.isnot(None),
        )
        .group_by(TraceSpan.currency)
        .all()
    )

    token_calls = int(totals["token_calls"] or 0)
    estimated_calls = int(totals["estimated_calls"] or 0)
    return {
        "rangeDays": window,
        "pricingConfigured": not price_table().empty,
        "totals": {
            "spans": int(totals["spans"] or 0),
            "turns": int(totals["turns"] or 0),
            "promptTokens": int(totals["prompt_tokens"] or 0),
            "completionTokens": int(totals["completion_tokens"] or 0),
            "failures": int(totals["failures"] or 0),
            # 估算占比越高，成本数字越应该当成量级参考而不是账单
            "estimatedTokenShare": (
                round(estimated_calls / token_calls, 4) if token_calls else None
            ),
        },
        "costs": [
            {"currency": row.currency, "amount": _as_float(row.amount)}
            for row in cost_rows
        ],
        "byName": _grouped(db, current_user.id, since, TraceSpan.name),
        "byModel": _grouped(db, current_user.id, since, TraceSpan.model),
        "byKind": _grouped(db, current_user.id, since, TraceSpan.kind),
        # 缓存统计存在进程内，重启归零，也不受 days 窗口约束——面板上要说清楚
        "cache": semantic_cache.stats(),
    }


@router.get("/traces")
async def list_traces(
    chat_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """最近若干次回答的概览，每条对应一棵 trace。"""
    query = db.query(
        TraceSpan.trace_id,
        TraceSpan.chat_id,
        TraceSpan.message_id,
        func.min(TraceSpan.started_at).label("started_at"),
        func.count(TraceSpan.id).label("spans"),
        func.coalesce(func.sum(TraceSpan.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(TraceSpan.completion_tokens), 0).label(
            "completion_tokens"
        ),
        func.sum(TraceSpan.cost).label("cost"),
        func.max(TraceSpan.currency).label("currency"),
        func.sum(case((TraceSpan.status != "ok", 1), else_=0)).label("failures"),
        # 根 span 的耗时就是这次回答的端到端时间
        func.max(
            case((TraceSpan.parent_id.is_(None), TraceSpan.duration_ms), else_=0)
        ).label("duration_ms"),
    ).filter(TraceSpan.user_id == current_user.id)
    if chat_id:
        query = query.filter(TraceSpan.chat_id == chat_id)

    rows = (
        query.group_by(TraceSpan.trace_id, TraceSpan.chat_id, TraceSpan.message_id)
        .order_by(func.min(TraceSpan.started_at).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "traceId": row.trace_id,
            "chatId": row.chat_id,
            "messageId": row.message_id,
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "durationMs": int(row.duration_ms or 0),
            "spans": int(row.spans),
            "promptTokens": int(row.prompt_tokens or 0),
            "completionTokens": int(row.completion_tokens or 0),
            "cost": _as_float(row.cost),
            "currency": row.currency,
            "failures": int(row.failures or 0),
        }
        for row in rows
    ]


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """单棵 trace 的完整 span 树。"""
    spans = (
        db.query(TraceSpan)
        .filter(TraceSpan.trace_id == trace_id, TraceSpan.user_id == current_user.id)
        .order_by(TraceSpan.started_at.asc())
        .all()
    )
    if not spans:
        raise HTTPException(status_code=404, detail="trace 不存在")

    nodes = {
        span.id: {
            "id": span.id,
            "parentId": span.parent_id,
            "name": span.name,
            "kind": span.kind,
            "startedAt": span.started_at.isoformat() if span.started_at else None,
            "durationMs": span.duration_ms,
            "status": span.status,
            "errorType": span.error_type,
            "model": span.model,
            "promptTokens": span.prompt_tokens,
            "completionTokens": span.completion_tokens,
            "tokenSource": span.token_source,
            "cost": _as_float(span.cost),
            "currency": span.currency,
            "attributes": span.attributes,
            "children": [],
        }
        for span in spans
    }

    roots: list[dict[str, Any]] = []
    for node in nodes.values():
        parent = nodes.get(node["parentId"]) if node["parentId"] else None
        # 父 span 不在结果里(理论上不该发生)时按根节点处理，避免整棵树丢失
        (parent["children"] if parent else roots).append(node)

    return {"traceId": trace_id, "roots": roots}
