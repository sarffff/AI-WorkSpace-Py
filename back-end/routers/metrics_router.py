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
from models import AgentRun, TraceSpan, User
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
            func.coalesce(func.sum(TraceSpan.cached_tokens), 0).label("cached_tokens"),
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
                # promptTokens 的子集,不是额外的量——前端不能把两者相加
                "cachedTokens": int(mapping["cached_tokens"] or 0),
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
            func.coalesce(func.sum(TraceSpan.cached_tokens), 0).label("cached_tokens"),
            # 只统计**有缓存信息**的那些调用的输入量,作为命中率的分母。
            # 拿全部 promptTokens 当分母会把老数据和本地估算的调用一起算进去,
            # 那些行的 cached_tokens 是 NULL,命中率会被系统性地压低。
            func.coalesce(
                func.sum(
                    case(
                        (TraceSpan.cached_tokens.isnot(None), TraceSpan.prompt_tokens),
                        else_=0,
                    )
                ),
                0,
            ).label("cacheable_prompt_tokens"),
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
    cached_tokens = int(totals["cached_tokens"] or 0)
    cacheable = int(totals["cacheable_prompt_tokens"] or 0)
    return {
        "rangeDays": window,
        "pricingConfigured": not price_table().empty,
        "totals": {
            "spans": int(totals["spans"] or 0),
            "turns": int(totals["turns"] or 0),
            "promptTokens": int(totals["prompt_tokens"] or 0),
            "completionTokens": int(totals["completion_tokens"] or 0),
            # promptTokens 中被提供商上下文缓存命中的部分。是子集不是增量
            "cachedTokens": cached_tokens,
            # 分母只算回传过缓存信息的调用；没有这类调用时给 None，
            # 面板上要显示"未知"而不是 0%——那是缺数据，不是没命中
            "promptCacheHitRate": (
                round(cached_tokens / cacheable, 4) if cacheable else None
            ),
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


@router.get("/agents")
async def get_agent_metrics(
    days: int = Query(default=0, ge=0, le=90),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """委派、审批、子代理的线上指标。

    这个接口存在的理由只有一个：**回答"委派到底值不值"**。多代理已经能跑，但
    "它比单代理好"到目前为止只是一个说法——没有任何数字支持它。委派的代价是
    确定的（每次一个完整的嵌套循环，成本与延迟都上去），收益是不确定的，
    而这个不确定性在任何单次回答里都看不出来。

    三个设计决定：

    **只按主代理 run 分桶（``parent_run_id IS NULL``）。** 子代理的成本要算进
    发起它的那次回答里，而不是单列一行——问题是"这次回答花了多少"，
    不是"researcher 花了多少"。子代理的钱通过 trace_id 自然滚进父 run。

    **委派 / 未委派的对比按 ``delegations > 0`` 切，而不是按开关模式切。**
    augment 模式下模型自己决定要不要派人，于是同一份配置里两种都有——
    这正是能看出委派有没有用的地方。按模式切只能比"两次不同配置的运行"，
    那里面混着别的变量。

    **成本从 ``trace_spans`` 聚合，不在 ``agent_runs`` 里再存一份。** 两份数字
    迟早不一致，而 trace 那边已经处理了按币种分组、provider/estimated 区分。
    代价是要 join，且埋点关掉时这一段为空——那时前端显示"未知"而不是 0。
    """
    window = days or settings.METRICS_DEFAULT_DAYS
    since = _window_start(window)

    base = (
        db.query(AgentRun)
        .filter(
            AgentRun.user_id == current_user.id,
            AgentRun.started_at >= since,
            AgentRun.parent_run_id.is_(None),
        )
        .subquery()
    )

    totals_row = db.query(
        func.count(base.c.id).label("runs"),
        func.sum(case((base.c.delegations > 0, 1), else_=0)).label("delegated"),
        func.coalesce(func.sum(base.c.delegations), 0).label("delegations"),
        func.coalesce(func.sum(base.c.interrupts), 0).label("interrupts"),
        func.sum(case((base.c.interrupts > 0, 1), else_=0)).label("interrupted_runs"),
        func.sum(case((base.c.status == "failed", 1), else_=0)).label("failed"),
        func.sum(case((base.c.status == "waiting_approval", 1), else_=0)).label(
            "waiting"
        ),
        func.avg(base.c.rounds).label("avg_rounds"),
    ).one()
    totals = totals_row._mapping
    runs = int(totals["runs"] or 0)

    # ---- 子代理：按角色分布与失败率 ----
    # 失败率按角色分开看:researcher 常失败和 critic 常失败是两种完全不同的病,
    # 前者多半是任务描述写得不够自包含,后者多半是它没拿到可审的材料。
    role_rows = (
        db.query(
            AgentRun.agent_role,
            func.count(AgentRun.id).label("runs"),
            func.sum(case((AgentRun.status == "failed", 1), else_=0)).label("failed"),
            func.avg(AgentRun.rounds).label("avg_rounds"),
        )
        .filter(
            AgentRun.user_id == current_user.id,
            AgentRun.started_at >= since,
            AgentRun.parent_run_id.isnot(None),
        )
        .group_by(AgentRun.agent_role)
        .order_by(func.count(AgentRun.id).desc())
        .all()
    )

    return {
        "rangeDays": window,
        # 快照关着的时候这张表根本不写行。返回 false 让前端说"未开启"，
        # 而不是画一个全零的面板——那两件事看起来一样，含义完全不同。
        "enabled": bool(settings.AGENT_CHECKPOINT_ENABLED),
        "delegationMode": settings.AGENT_DELEGATION_MODE,
        "approvalMode": settings.AGENT_APPROVAL_MODE,
        "totals": {
            "runs": runs,
            "delegatedRuns": int(totals["delegated"] or 0),
            "delegations": int(totals["delegations"] or 0),
            # 分母是主代理 run 数。没有 run 时给 None——0% 会被读成
            # "从来不委派"，而真相是"这个窗口里什么都没跑"
            "delegationRate": (
                round(int(totals["delegated"] or 0) / runs, 4) if runs else None
            ),
            "interrupts": int(totals["interrupts"] or 0),
            "interruptedRuns": int(totals["interrupted_runs"] or 0),
            "failedRuns": int(totals["failed"] or 0),
            "waitingApproval": int(totals["waiting"] or 0),
            "avgRounds": _as_float(totals["avg_rounds"]),
        },
        "byRole": [
            {
                "role": row.agent_role,
                "runs": int(row.runs),
                "failed": int(row.failed or 0),
                "failureRate": (
                    round(int(row.failed or 0) / int(row.runs), 4) if row.runs else None
                ),
                "avgRounds": _as_float(row.avg_rounds),
            }
            for row in role_rows
        ],
        "comparison": _delegation_comparison(db, current_user.id, since),
    }


def _delegation_comparison(
    db: Session, user_id: str, since: datetime
) -> list[dict[str, Any]]:
    """委派 vs 未委派的轮次、成本、延迟对比。

    这是整个面板真正要看的那张表。判断委派值不值只能靠它——单看"委派率 27%"
    什么都说明不了，得知道那 27% 多花了几倍的钱、慢了几倍。

    成本按 ``(是否委派, 币种)`` 分组:不同模型可能用不同货币,把 CNY 和 USD
    相加是错的（与 ``/usage`` 同一套约定）。
    """
    rows = (
        db.query(
            case((AgentRun.delegations > 0, 1), else_=0).label("delegated"),
            TraceSpan.currency,
            func.count(func.distinct(AgentRun.id)).label("runs"),
            func.avg(AgentRun.rounds).label("avg_rounds"),
            func.sum(TraceSpan.cost).label("cost"),
            func.coalesce(func.sum(TraceSpan.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(TraceSpan.completion_tokens), 0).label(
                "completion_tokens"
            ),
        )
        # inner join：埋点关掉时 trace_id 为空，这一段自然为空列表，
        # 前端据此显示"需要开启埋点"而不是一堆 0
        .join(TraceSpan, TraceSpan.trace_id == AgentRun.trace_id)
        .filter(
            AgentRun.user_id == user_id,
            AgentRun.started_at >= since,
            AgentRun.parent_run_id.is_(None),
        )
        .group_by(case((AgentRun.delegations > 0, 1), else_=0), TraceSpan.currency)
        .all()
    )

    # 延迟单列一次查询：它要的是**每个 run 的总耗时**（根 span 的 duration），
    # 而上面那个 join 是逐 span 的行——在同一个查询里 avg(duration_ms) 算出来的
    # 是"平均每个 span 多久"，不是"平均每次回答多久"。这两个数差一个数量级，
    # 而它们看起来都像是延迟。
    latency_rows = (
        db.query(
            case((AgentRun.delegations > 0, 1), else_=0).label("delegated"),
            func.avg(TraceSpan.duration_ms).label("avg_ms"),
        )
        .join(TraceSpan, TraceSpan.trace_id == AgentRun.trace_id)
        .filter(
            AgentRun.user_id == user_id,
            AgentRun.started_at >= since,
            AgentRun.parent_run_id.is_(None),
            TraceSpan.name == "chat.turn",
            TraceSpan.parent_id.is_(None),
        )
        .group_by(case((AgentRun.delegations > 0, 1), else_=0))
        .all()
    )
    latency = {int(row.delegated): _as_float(row.avg_ms) for row in latency_rows}

    return [
        {
            "delegated": bool(row.delegated),
            "currency": row.currency,
            "runs": int(row.runs),
            "avgRounds": _as_float(row.avg_rounds),
            "cost": _as_float(row.cost),
            "avgCost": (
                round(float(row.cost) / int(row.runs), 6)
                if row.cost is not None and row.runs
                else None
            ),
            "promptTokens": int(row.prompt_tokens or 0),
            "completionTokens": int(row.completion_tokens or 0),
            "avgTurnMs": latency.get(int(row.delegated)),
        }
        for row in rows
    ]


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
        func.coalesce(func.sum(TraceSpan.cached_tokens), 0).label("cached_tokens"),
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
            "cachedTokens": int(row.cached_tokens or 0),
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
            "cachedTokens": span.cached_tokens,
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
