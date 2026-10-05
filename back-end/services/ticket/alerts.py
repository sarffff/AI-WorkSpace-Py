"""工单侧的"有事等你"通知。

为什么要有这一个文件：``models.Notification`` 的文档串写得很清楚——审批挂起这类
事件原本**只在那条实时 SSE 连接上存在一瞬**，刷新、切页、断网之后就没了，审批人
只能靠轮询重新发现。工单这条路径比 chat 更依赖人在场：一次资金审批可能挂一整天，
而那一整天里没有任何连接活着。不通知，文档§6.2 的人在回路就只是"等人恰好打开页面"。

收件人的确定方式：

1. 有受理人就发给受理人。这是唯一的指向性信号——本仓库没有细粒度权限，
   ``role`` 只区分能不能改组织资产，不能用来回答"这单该谁接"。
2. 没有受理人时发给该工作区的管理员。**发给管理员而不是发给全工作区所有人**：
   通知是拉取式的 per-user 收件箱，广播出去就是让每个人都收到一条"有件事也许
   该你管，但我们不知道"，而刷屏三次之后他们就开始无视它——那时候真正等着的
   审批也被无视了。

通知写失败不阻断主流程：``notification_service.create`` 自己就是失败只记日志的
（它是旁路记录，不能让一次通知写失败把已经挂起的工单也带崩）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from models import Ticket, User
from services.notification_service import notification_service

KIND_APPROVAL = "approval_required"
KIND_HANDOFF = "ticket_handoff"


def recipients(db: Session, ticket: Ticket) -> list[str]:
    if ticket.assignee_id:
        return [ticket.assignee_id]
    if not ticket.workspace_id:
        return []
    return [
        user.id
        for user in db.query(User)
        .filter(User.workspace_id == ticket.workspace_id, User.role == "admin")
        .all()
    ]


def _notify(db: Session, ticket: Ticket, *, kind: str, title: str, body: str) -> int:
    sent = 0
    for user_id in recipients(db, ticket):
        if notification_service.create(
            db,
            user_id=user_id,
            kind=kind,
            title=title[:255],
            body=body,
            ticket_id=ticket.id,
        ):
            sent += 1
    if sent == 0 and not ticket.assignee_id:
        # "没人可发"是一件需要被看见的事：一张卡在审批上的工单如果既没有受理人
        # 也没有管理员，它就会安静地躺在那里，而所有指标都显示正常
        from services.ticket.trace import append_event

        append_event(
            db,
            ticket,
            node="escalate",
            kind="error",
            status="blocked",
            message="没有可通知的人（工单无受理人，工作区无管理员），这件事不会自己被人发现",
        )
    return sent


def notify_approval_needed(db: Session, ticket: Ticket, pending: dict | None = None) -> int:
    calls = (pending or {}).get("calls") or []
    names = "、".join(str(item.get("name")) for item in calls) or "一次待批操作"
    reason = (pending or {}).get("reason") or "资金类操作必须人工确认"
    return _notify(
        db,
        ticket,
        kind=KIND_APPROVAL,
        title=f"工单等你批准：{ticket.subject or ticket.id}",
        body=(
            f"{reason}。待批操作：{names}。"
            "批准后才会真正执行；不同意可以拒绝或改参数后再同意。"
        ),
    )


def notify_handoff(db: Session, ticket: Ticket, reason: str) -> int:
    return _notify(
        db,
        ticket,
        kind=KIND_HANDOFF,
        title=f"工单转人工：{ticket.subject or ticket.id}",
        body=(
            f"原因：{reason}。之前几步查到过什么已经写进这张工单的轨迹，"
            "处理完请在工单台点「人工关单」，否则它还挂在待办里。"
        ),
    )
