"""线上健康监控：从 tickets + trace_spans 聚合真实指标，越阈值经通知出口告警。

``usage_guard`` 防的是**单个用户**跑飞，它答不了另一个问题：整条线**作为一个整体**
是不是在变坏——错误率爬升、人工介入变多、单张工单变贵。离线评估（eval + 门禁）量的
是"模型在固定题上的质量"，但它跑在温度 0、固定语料上，和线上（真实流量）是两套分布。
这里补的就是"线上这半环"。

**机制而非话术**：阈值是代码里的数字判断，告警是一条真的 Notification，不是在提示词
里写"请注意质量"。节流 + 去重让它能挂在高频读路径上而不刷屏（见 config 的 MONITOR_*）。

计数单位是**工单**而不是"一次回答"：工单是这个产品的计数单位，也是文档里那五个核心
指标（自动解决率、首次响应、平均处理时长、CSAT、错误操作率）的分母所在。一次编排
可能跨天、被审批打断好几次，按回答计数会把"一张工单被反复折腾"这种最该看见的劣化
平均掉。

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
from models import Notification, Ticket, User
from services.clock import naive_now
from services.notification_service import notification_service

logger = logging.getLogger("production_monitor")

_ALERT_KIND = "health_alert"

# 节流状态：进程内最近一次真正评估的时刻。多 worker 下各有一份（见模块文档）。
_last_eval_at: datetime | None = None
_lock = threading.Lock()

_LABELS = {
    "errorRate": "失败率",
    "interventionRate": "人工介入率",
    "avgCostPerTicket": "单工单平均成本",
    "p95HandleMs": "平均处理时长 p95(ms)",
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


# 走到人手里的状态：等审批 = 已经挂了人在回路；escalated = 已经转人工。
# 两者都要算进"人工介入率"——文档里"人工坐席工作量下降比例"这个指标，
# 分母减少的唯一来源就是这两类变少。
_HUMAN_STATUSES = ("awaiting_approval", "escalated")


def health_report(
    db: Session, *, window_hours: float | None = None, min_tickets: int | None = None
) -> dict:
    """窗口内的全局健康快照。只读，不发告警（告警在 evaluate_and_alert）。

    成本取 ``tickets.llm_cost``，它在编排每步之后累加、挂起时也会落一次，所以
    "还没办完但已经花了三块钱"的工单也在数里。NULL 表示这次运行没拿到用量
    （"未知"不是 0，同 pricing 的约定），不计入均值也不计入分位。
    """
    window_hours = window_hours if window_hours is not None else settings.MONITOR_WINDOW_HOURS
    min_tickets = (
        settings.MONITOR_MIN_TICKETS if min_tickets is None else min_tickets
    )
    since = naive_now() - timedelta(hours=window_hours)

    tickets = db.query(Ticket).filter(Ticket.created_at >= since).all()
    total = len(tickets)
    report: dict = {
        "windowHours": window_hours,
        "totalTickets": total,
        "sufficient": total >= min_tickets,
        "metrics": {},
        "breaches": [],
    }
    if total == 0:
        return report

    failed = sum(1 for t in tickets if t.status == "failed")
    intervened = sum(1 for t in tickets if t.status in _HUMAN_STATUSES)
    error_rate = failed / total
    intervention_rate = intervened / total

    handle_ms = [
        (t.resolved_at - t.created_at).total_seconds() * 1000.0
        for t in tickets
        if t.resolved_at and t.created_at
    ]
    p95_handle = _percentile(handle_ms, 95)

    costs = [float(t.llm_cost) for t in tickets if t.llm_cost is not None]
    avg_cost = sum(costs) / len(costs) if costs else None
    p95_cost = _percentile(costs, 95)

    report["metrics"] = {
        "errorRate": round(error_rate, 4),
        "interventionRate": round(intervention_rate, 4),
        "failedTickets": failed,
        "intervenedTickets": intervened,
        "avgCostPerTicket": round(avg_cost, 6) if avg_cost is not None else None,
        "p95CostPerTicket": round(p95_cost, 6) if p95_cost is not None else None,
        "p95HandleMs": round(p95_handle) if p95_handle is not None else None,
        "ticketsWithKnownCost": len(costs),
    }
    # 阈值判定只在样本够时做——三五个工单的比例是噪声不是信号。
    if report["sufficient"]:
        report["breaches"] = _breaches(
            error_rate, intervention_rate, avg_cost, p95_handle
        )
    return report


def _breach(metric: str, value: float, threshold: float) -> dict:
    return {"metric": metric, "value": round(value, 4), "threshold": threshold}


def _breaches(error_rate, intervention_rate, avg_cost, p95_handle) -> list[dict]:
    """各阈值独立判，0 = 关掉这一条（同 usage_guard 的约定）。"""
    out: list[dict] = []
    if 0 < settings.MONITOR_MAX_ERROR_RATE < error_rate:
        out.append(_breach("errorRate", error_rate, settings.MONITOR_MAX_ERROR_RATE))
    if 0 < settings.MONITOR_MAX_INTERVENTION_RATE < intervention_rate:
        out.append(
            _breach("interventionRate", intervention_rate, settings.MONITOR_MAX_INTERVENTION_RATE)
        )
    if avg_cost is not None and 0 < settings.MONITOR_MAX_COST_PER_TICKET < avg_cost:
        out.append(
            _breach("avgCostPerTicket", avg_cost, settings.MONITOR_MAX_COST_PER_TICKET)
        )
    if p95_handle is not None and 0 < settings.MONITOR_MAX_P95_HANDLE_MS < p95_handle:
        out.append(_breach("p95HandleMs", p95_handle, settings.MONITOR_MAX_P95_HANDLE_MS))
    return out


def _format_body(report: dict) -> str:
    lines = [f"窗口 {report['windowHours']} 小时内 {report['totalTickets']} 张工单："]
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
