"""线上健康监控：从 trace_spans + agent_runs 聚合真实指标，越阈值经 B1 通知出口告警。

``usage_guard`` 防的是**单个用户**跑飞，它答不了另一个问题：整条线**作为一个整体**
是不是在变坏——错误率爬升、人工介入变多、单次回答变贵。离线金标（eval + 门禁）量的
是"模型在固定题上的质量"，但它跑在温度 0、固定语料上，和线上（温度 0.7、真实流量）
是两套分布。这里补的就是"线上这半环"。

**机制而非话术**：阈值是代码里的数字判断，告警是一条真的 Notification，不是在提示词
里写"请注意质量"。节流 + 去重让它能挂在高频读路径上而不刷屏（见 config 的 MONITOR_*）。

没有内置定时器（项目里没有调度器）。评估顺带挂在**通知未读数轮询**上（前端徽标心跳），
也可以让外部 cron 打 ``GET /metrics/health``。多 worker 下每个 worker 各评估一次，靠
"每个管理员同时只留一条未读健康告警"去重吸收重复——与 VECTOR_STORE=memory 同一类
诚实边界。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from config import settings
from models import AgentRun, Notification, TraceSpan, User
from services.clock import naive_now
from services.notification_service import notification_service

logger = logging.getLogger("production_monitor")

_ALERT_KIND = "health_alert"

# 节流状态：进程内最近一次真正评估的时刻。多 worker 下各有一份（见模块文档）。
_last_eval_at: datetime | None = None
_lock = threading.Lock()

_LABELS = {
    "errorRate": "错误率",
    "interventionRate": "人工介入率",
    "avgCostPerRun": "单次回答平均成本",
    "p95LatencyMs": "端到端 p95 延迟(ms)",
}


def _percentile(values: list[float], p: float) -> float | None:
    """线性插值分位。空列表给 None（"未知"，不是 0）。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def health_report(
    db: Session, *, window_hours: float | None = None, min_runs: int | None = None
) -> dict:
    """窗口内的全局健康快照。只读，不发告警（告警在 evaluate_and_alert）。

    只看主代理 run（``parent_run_id IS NULL``）：子代理失败是内部的，用户看到的
    粒度是"一次回答"。成本按 run 的 trace_id 聚合 trace_spans.cost，NULL 不计入
    （"未知"不是 0，同 pricing 的约定）。
    """
    window_hours = window_hours if window_hours is not None else settings.MONITOR_WINDOW_HOURS
    min_runs = settings.MONITOR_MIN_RUNS if min_runs is None else min_runs
    since = naive_now() - timedelta(hours=window_hours)

    runs = (
        db.query(AgentRun)
        .filter(AgentRun.parent_run_id.is_(None), AgentRun.started_at >= since)
        .all()
    )
    total = len(runs)
    report: dict = {
        "windowHours": window_hours,
        "totalRuns": total,
        "sufficient": total >= min_runs,
        "metrics": {},
        "breaches": [],
    }
    if total == 0:
        return report

    failed = sum(1 for r in runs if r.status == "failed")
    intervened = sum(
        1 for r in runs if (r.interrupts or 0) > 0 or r.status == "abandoned"
    )
    error_rate = failed / total
    intervention_rate = intervened / total

    latencies = [
        (r.finished_at - r.started_at).total_seconds() * 1000.0
        for r in runs
        if r.finished_at and r.started_at
    ]
    p95_latency = _percentile(latencies, 95)

    costs = _run_costs(db, [r.trace_id for r in runs if r.trace_id])
    avg_cost = sum(costs) / len(costs) if costs else None
    p95_cost = _percentile(costs, 95)

    report["metrics"] = {
        "errorRate": round(error_rate, 4),
        "interventionRate": round(intervention_rate, 4),
        "failedRuns": failed,
        "intervenedRuns": intervened,
        "avgCostPerRun": round(avg_cost, 6) if avg_cost is not None else None,
        "p95CostPerRun": round(p95_cost, 6) if p95_cost is not None else None,
        "p95LatencyMs": round(p95_latency) if p95_latency is not None else None,
        "runsWithKnownCost": len(costs),
    }
    # 阈值判定只在样本够时做——三五个 run 的比例是噪声不是信号。
    if report["sufficient"]:
        report["breaches"] = _breaches(error_rate, intervention_rate, avg_cost, p95_latency)
    return report


def _run_costs(db: Session, trace_ids: list[str]) -> list[float]:
    """每个 run 的总成本（按 trace_id 聚合 trace_spans.cost）。

    混币种时直接相加——有汇率问题,所以 MONITOR_MAX_COST_PER_RUN 默认 0(关)。
    单币种部署(绝大多数)下这是对的;混币种部署该把成本阈值留 0,看错误率/延迟那几条。
    """
    from sqlalchemy import func

    if not trace_ids:
        return []
    rows = (
        db.query(TraceSpan.trace_id, func.sum(TraceSpan.cost))
        .filter(TraceSpan.trace_id.in_(trace_ids), TraceSpan.cost.isnot(None))
        .group_by(TraceSpan.trace_id)
        .all()
    )
    return [float(amount) for _tid, amount in rows if amount is not None]


def _breach(metric: str, value: float, threshold: float) -> dict:
    return {"metric": metric, "value": round(value, 4), "threshold": threshold}


def _breaches(error_rate, intervention_rate, avg_cost, p95_latency) -> list[dict]:
    """各阈值独立判，0 = 关掉这一条（同 usage_guard 的约定）。"""
    out: list[dict] = []
    if 0 < settings.MONITOR_MAX_ERROR_RATE < error_rate:
        out.append(_breach("errorRate", error_rate, settings.MONITOR_MAX_ERROR_RATE))
    if 0 < settings.MONITOR_MAX_INTERVENTION_RATE < intervention_rate:
        out.append(
            _breach("interventionRate", intervention_rate, settings.MONITOR_MAX_INTERVENTION_RATE)
        )
    if avg_cost is not None and 0 < settings.MONITOR_MAX_COST_PER_RUN < avg_cost:
        out.append(_breach("avgCostPerRun", avg_cost, settings.MONITOR_MAX_COST_PER_RUN))
    if p95_latency is not None and 0 < settings.MONITOR_MAX_P95_LATENCY_MS < p95_latency:
        out.append(_breach("p95LatencyMs", p95_latency, settings.MONITOR_MAX_P95_LATENCY_MS))
    return out


def _format_body(report: dict) -> str:
    lines = [f"窗口 {report['windowHours']} 小时内 {report['totalRuns']} 次回答："]
    for breach in report["breaches"]:
        label = _LABELS.get(breach["metric"], breach["metric"])
        lines.append(f"- {label}：{breach['value']}（阈值 {breach['threshold']}）")
    return "\n".join(lines)


def evaluate_and_alert(db: Session, *, force: bool = False) -> list[str]:
    """节流评估 + 越阈值给管理员发一条去重告警。非阻塞：任何异常只记日志。

    返回本次**新建**的通知 id（没有新建则空）。``force=True`` 跳过节流（/metrics/health
    与测试用）。关着(MONITOR_ENABLED=false)时直接空手返回。
    """
    global _last_eval_at
    if not settings.MONITOR_ENABLED:
        return []
    now = naive_now()
    if not force:
        # 节流在锁内：间隔内的并发调用里只有第一个会往下走，其余立刻返回。
        with _lock:
            if _last_eval_at is not None and now - _last_eval_at < timedelta(
                minutes=settings.MONITOR_INTERVAL_MINUTES
            ):
                return []
            _last_eval_at = now
    try:
        report = health_report(db)
        if not report["breaches"]:
            return []
        return _fan_out(db, report)
    except Exception:
        # 监控是旁路：评估/告警失败绝不能把它挂着的那个读请求也带崩。
        logger.exception("health evaluate_and_alert failed")
        return []


def _fan_out(db: Session, report: dict) -> list[str]:
    """给每个在岗管理员发一条健康告警，已有未读的跳过（已读后再超阈值才算新事件）。"""
    admins = (
        db.query(User)
        .filter(User.role == "admin", User.is_active.is_(True))
        .all()
    )
    if not admins:
        logger.warning(
            "health breach but no active admin to notify: %s",
            [b["metric"] for b in report["breaches"]],
        )
        return []

    title = f"系统健康告警：{len(report['breaches'])} 项指标超阈值"
    body = _format_body(report)
    created: list[str] = []
    for admin in admins:
        existing = (
            db.query(Notification.id)
            .filter(
                Notification.user_id == admin.id,
                Notification.kind == _ALERT_KIND,
                Notification.read_at.is_(None),
            )
            .first()
        )
        if existing is not None:
            continue  # 该管理员已有未读健康告警，别叠加刷屏
        nid = notification_service.create(
            db, user_id=admin.id, kind=_ALERT_KIND, title=title, body=body
        )
        if nid:
            created.append(nid)
    return created
