"""MCP 工具面：注册、schema 一致性，以及幂等跨协议边界仍然成立。

走的是 ``MCPServer.list_tools()`` / ``call_tool()``——传输层（stdio/HTTP）下面
被调用的就是这两个方法，所以这一层测的是"工具注册对不对、参数怎么派生 schema、
业务拒绝怎么回来"。真正的 JSON-RPC 成帧与进程启动不在这里覆盖：那是 SDK 自己的
测试面，把它接进来只会让这条测试变成对 SDK 的重复劳动。

**schema 一致性那条断言是这个文件存在的另一半理由。** 进程内那套的 schema 来自
Pydantic 模型，而 MCP 的 schema 从函数签名推导，两份定义天生会漂；漂移的后果是
一个客户端按 MCP 描述传参、服务端按另一套校验——那不会报错，只会静默地行为不同。
"""
import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, pool
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401
from database import Base
from models import CsOrder, CsRefund
from services.clock import naive_now
from services.ticket import mcp_server, tools as cs

WS = "w1"

# MCP 这一侧必须显式出现在签名里的上下文参数：进程内是 build() 闭包绑好的，
# 无状态的 server 只能让调用方自己说清楚
_CONTEXT_FIELDS = {
    "lookup_order": {"workspace_id"},
    "lookup_logistics": {"workspace_id"},
    "lookup_customer": {"workspace_id"},
    "update_order_address": {"workspace_id", "actor", "ticket_id"},
    "request_invoice": {"workspace_id", "actor", "ticket_id"},
    "create_refund": {"workspace_id", "actor", "ticket_id", "approved_by"},
    "cancel_order": {"workspace_id", "actor", "ticket_id"},
}

_ARGS_MODELS = {
    "lookup_order": cs.LookupOrderArgs,
    "lookup_logistics": cs.LookupLogisticsArgs,
    "lookup_customer": cs.LookupCustomerArgs,
    "update_order_address": cs.UpdateAddressArgs,
    "request_invoice": cs.RequestInvoiceArgs,
    "create_refund": cs.CreateRefundArgs,
    "cancel_order": cs.CancelOrderArgs,
}


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=pool.StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    now = naive_now()
    session = factory()
    session.add(
        CsOrder(
            id="ord1", workspace_id=WS, order_no="ORD-1001", status="paid",
            total_amount=Decimal("350.00"), refunded_amount=Decimal("0.00"),
            receiver_name="张三", address_text="北京市海淀区中关村大街1号",
            created_at=now, updated_at=now,
        )
    )
    session.commit()
    session.close()
    yield factory
    engine.dispose()


@pytest.fixture()
def server(db_factory):
    return mcp_server.build_server(db_factory)


def _text(result) -> str:
    """CallToolResult 里的正文。structuredContent 是同一段话的另一份形状，不重复取。"""
    return "".join(block.text for block in result.content if getattr(block, "text", None))


def test_七个业务工具都注册上了(server):
    names = [tool.name for tool in asyncio.run(server.list_tools())]
    assert names == list(cs.TIER_BY_TOOL)


def test_mcp上报的schema与进程内的模型定义对得上(server):
    """这两处分别由函数签名和 Pydantic 模型生成，漂移不会报错，只会行为不同。"""
    by_name = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name, model in _ARGS_MODELS.items():
        schema = by_name[name].input_schema
        expected = set(model.model_json_schema()["properties"]) | _CONTEXT_FIELDS[name]
        assert set(schema["properties"]) == expected, name
        # 必填那侧也一样：模型里没默认值的字段 + MCP 侧必填的上下文
        model_required = set(model.model_json_schema().get("required", []))
        required = set(schema.get("required", []))
        assert required == model_required | (
            {"workspace_id", "actor"} if "actor" in expected else {"workspace_id"}
        ), name


def test_工具说明与进程内是同一句话(server):
    by_name = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name, description in cs._DESCRIPTIONS.items():
        assert by_name[name].description == description


def test_通过MCP查订单拿到的是同一份业务事实(server):
    result = asyncio.run(
        server.call_tool("lookup_order", {"workspace_id": WS, "order_no": "ORD-1001"})
    )
    assert "可退 350.00 元" in _text(result)


def test_业务拒绝以文本回来而不是让调用方拿到协议错误(server):
    result = asyncio.run(
        server.call_tool("lookup_order", {"workspace_id": WS, "order_no": "NOPE"})
    )
    assert "查无此单" in _text(result)
    assert result.is_error is not True


def test_跨MCP重复提交同一笔退款只退一次(server, db_factory):
    args = {
        "workspace_id": WS,
        "actor": "seat-1",
        "ticket_id": "t-1",
        "order_no": "ORD-1001",
        "amount": "350",
    }
    first = _text(asyncio.run(server.call_tool("create_refund", dict(args))))
    assert "退款已发起" in first
    second = _text(asyncio.run(server.call_tool("create_refund", dict(args))))
    assert second.startswith("重复调用")

    session = db_factory()
    try:
        assert session.query(CsRefund).count() == 1
        assert session.query(CsOrder).filter_by(order_no="ORD-1001").one().refunded_amount == Decimal("350.00")
    finally:
        session.close()


def test_换一个工单号就是新的一笔而不是重复(server, db_factory):
    """幂等键按 (工单, 操作, 参数) 推导：无状态的调用方必须有办法说明"这是新的一单"。"""
    base = {
        "workspace_id": WS,
        "actor": "seat-1",
        "order_no": "ORD-1001",
        "amount": "100",
    }
    _text(asyncio.run(server.call_tool("create_refund", dict(base, ticket_id="t-1"))))
    _text(asyncio.run(server.call_tool("create_refund", dict(base, ticket_id="t-2", amount="50"))))
    session = db_factory()
    try:
        assert session.query(CsRefund).count() == 2
    finally:
        session.close()


def test_会话工厂注入意味着这个server在没有数据库的机器上也能被问话(db_factory):
    """不注入就得在 import 期连库，而 tool/list 本来不需要任何业务数据。"""
    server = mcp_server.build_server(db_factory)
    assert len(asyncio.run(server.list_tools())) == len(cs.TIER_BY_TOOL)
