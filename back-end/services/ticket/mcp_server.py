"""把工单业务工具经 MCP 暴露出去（文档第 3 层与§5 的"协议"那一行）。

## 为什么这一层值得存在

文档的说法是所有工具统一经 MCP Server 暴露。真实部署里订单中心、CRM、支付网关
是别人的系统，Agent 这边不该知道它们的表结构；而反过来，别的 Agent（或公司里
任何一个说 MCP 的客户端）应当能在不读本仓库代码的前提下调用同一套业务能力。
MCP 就是这个边界的**名字**，这一层是它的落地。

## 为什么工具定义看起来写了两遍

进程内那一套（``services/ticket/tools.py``）的 schema 来自 Pydantic 模型，而 MCP
的 SDK 只从**函数签名**推导输入 schema，不接受外部传入的 schema。所以这里的每个
工具都显式写了标量参数——业务校验仍然在 ``cs.*`` 函数里，这一层只做接线。

这两份定义会漂移的风险由 ``tests/test_ticket_mcp_server.py`` 里一条断言钉住：
MCP 上报的 inputSchema 必须等于 Pydantic 模型生成的 schema 加上上下文那几个字段。
不一致就直接失败。这是本仓库既有的做法（``services/approval.py`` 里那两个字符串
元组的一致性也是靠测试而不是靠共享常量守住的），比为了消除重复去写一层
"从模型反向生成函数签名"的机器便宜得多，也比放任两边各长各的诚实得多。

## 上下文参数为什么显式出现在签名里

``workspace_id`` / ``actor`` / ``ticket_id`` 在进程内是 ``build()`` 闭包绑好的，
而 MCP 服务端是无状态的：一次 tool/call 必须自己说清楚"为哪个工作区、以谁的身份、
挂在哪张工单上"。这不是冗余——**幂等键是按工单推导的**，不带 ticket_id 的两次调用
会被当成同一个客户的新一次尝试，那正是重复退款的形状。

## 会话工厂是可注入的

``build_server(session_factory=...)``。生产用 ``database.SessionLocal``，测试和评估
注入一个内存 SQLite 的工厂。不做这件事的话，这个 server 在没起数据库的机器上
连一次 tool/list 都跑不了，而那是它最该被验证的时刻。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from services.ticket import tools as cs

logger = logging.getLogger("ticket.mcp")

SERVER_NAME = "customer-service"

_INSTRUCTIONS = (
    "客服工单解决 Agent 的业务工具面。权限分三档：查询（read）、修改（mutate）、"
    "资金（fund）。资金类操作必须已经取得人工批准才有意义；所有写操作按 "
    "(工单, 操作, 参数) 推导幂等键，重复提交同一笔不会执行两次。"
)


def build_server(session_factory: Callable[[], Session]) -> Any:
    """装配 MCP server。每次 tool/call 自己开一个会话并负责关掉。"""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name=SERVER_NAME,
        instructions=_INSTRUCTIONS,
    )

    def session() -> Session:
        db = session_factory()
        return db

    # ---- 查询档 ----

    @server.tool(
        name="lookup_order",
        description=cs._DESCRIPTIONS["lookup_order"],
    )
    def _lookup_order(workspace_id: str, order_no: str) -> str:
        db = session()
        try:
            return cs.lookup_order(db, workspace_id, order_no=order_no)
        except cs.BusinessReject as exc:
            return exc.message
        finally:
            db.close()

    @server.tool(
        name="lookup_logistics",
        description=cs._DESCRIPTIONS["lookup_logistics"],
    )
    def _lookup_logistics(workspace_id: str, order_no: str = "", tracking_no: str = "") -> str:
        db = session()
        try:
            return cs.lookup_logistics(
                db, workspace_id, order_no=order_no, tracking_no=tracking_no
            )
        except cs.BusinessReject as exc:
            return exc.message
        finally:
            db.close()

    @server.tool(
        name="lookup_customer",
        description=cs._DESCRIPTIONS["lookup_customer"],
    )
    def _lookup_customer(
        workspace_id: str, customer_id: str = "", email: str = "", phone: str = ""
    ) -> str:
        db = session()
        try:
            return cs.lookup_customer(
                db, workspace_id, customer_id=customer_id, email=email, phone=phone
            )
        except cs.BusinessReject as exc:
            return exc.message
        finally:
            db.close()

    # ---- 修改档 ----

    @server.tool(
        name="update_order_address",
        description=cs._DESCRIPTIONS["update_order_address"],
    )
    def _update_order_address(
        workspace_id: str,
        actor: str,
        order_no: str,
        receiver_name: str = "",
        receiver_phone: str = "",
        address_text: str = "",
        ticket_id: str = "",
        idempotency_key: str = "",
    ) -> str:
        db = session()
        try:
            return cs.update_order_address(
                db,
                workspace_id,
                order_no=order_no,
                receiver_name=receiver_name,
                receiver_phone=receiver_phone,
                address_text=address_text,
                idempotency_key=idempotency_key,
                ticket_id=ticket_id or None,
                actor=actor,
            )
        except cs.BusinessReject as exc:
            return exc.message
        finally:
            db.close()

    @server.tool(
        name="request_invoice",
        description=cs._DESCRIPTIONS["request_invoice"],
    )
    def _request_invoice(
        workspace_id: str,
        actor: str,
        order_no: str,
        title: str,
        tax_no: str = "",
        email: str = "",
        ticket_id: str = "",
        idempotency_key: str = "",
    ) -> str:
        db = session()
        try:
            return cs.request_invoice(
                db,
                workspace_id,
                order_no=order_no,
                title=title,
                tax_no=tax_no,
                email=email,
                idempotency_key=idempotency_key,
                ticket_id=ticket_id or None,
                actor=actor,
            )
        except cs.BusinessReject as exc:
            return exc.message
        finally:
            db.close()

    # ---- 资金档 ----
    #
    # MCP 层不判断"有没有人批准"——它没有那个上下文（批准发生在编排图的挂起点上）。
    # 所以这两个工具在描述里把"必须已经拿到人工批准"写清楚，让任何客户端在调用之前
    # 自己负责；而真正的闸门仍然在图里。这里刻意不做的事：在 server 上再放一套
    # 审批状态，那会让"谁批的"这件事出现第二个真相。

    @server.tool(
        name="create_refund",
        description=cs._DESCRIPTIONS["create_refund"],
    )
    def _create_refund(
        workspace_id: str,
        actor: str,
        order_no: str,
        amount: str,
        reason_code: str = "",
        approved_by: str = "",
        ticket_id: str = "",
        idempotency_key: str = "",
    ) -> str:
        db = session()
        try:
            return cs.create_refund(
                db,
                workspace_id,
                order_no=order_no,
                amount=amount,
                reason_code=reason_code,
                approved_by=approved_by or None,
                idempotency_key=idempotency_key,
                ticket_id=ticket_id or None,
                actor=actor,
            )
        except cs.BusinessReject as exc:
            return exc.message
        finally:
            db.close()

    @server.tool(
        name="cancel_order",
        description=cs._DESCRIPTIONS["cancel_order"],
    )
    def _cancel_order(
        workspace_id: str,
        actor: str,
        order_no: str,
        reason: str = "",
        ticket_id: str = "",
        idempotency_key: str = "",
    ) -> str:
        db = session()
        try:
            return cs.cancel_order(
                db,
                workspace_id,
                order_no=order_no,
                reason=reason,
                idempotency_key=idempotency_key,
                ticket_id=ticket_id or None,
                actor=actor,
            )
        except cs.BusinessReject as exc:
            return exc.message
        finally:
            db.close()

    return server


def default_server() -> Any:
    from database import SessionLocal

    return build_server(SessionLocal)


if __name__ == "__main__":
    import asyncio

    # stdio 是本地 MCP 客户端（桌面端的 agent、开发期的 inspector）默认的连接方式。
    # 要挂到网关上就换成 run("streamable-http")，那是部署决定而不是这里的。
    asyncio.run(default_server().run("stdio"))
