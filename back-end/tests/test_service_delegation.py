"""服务层委派场景测试（E）：SSE 契约已覆盖事件序列，这里补三个行为缺口。

1. 越权工具：子代理只能调用自己角色的工具，编造的工具名在 schema 过滤之后
   还有一层执行前拦截，且绝不落到真实执行；
2. 轮次上限：analyst 最多 3 轮，第 3 轮强制收报告并标记 truncated；
3. 共享预算：子代理与主代理共用同一份工具结果字符预算，子代理吃掉的额度
   主代理看得见，预算耗尽后主代理直接收敛。
"""
from __future__ import annotations

from conftest import collect, run
from config import settings

from tests.test_sse_contract import make_service
from tests.test_service_security import RecordingKnowledge


def _enable_delegation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    monkeypatch.setattr(settings, "AGENT_MAX_DELEGATIONS", 3)
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "TOOL_CALCULATE_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_WEB_SEARCH_ENABLED", False)
    monkeypatch.setattr(settings, "TOOL_READ_ATTACHMENT_ENABLED", False)
    monkeypatch.setattr(settings, "TOOL_WRITE_KNOWLEDGE_ENABLED", True)


def test_subagent_unauthorized_tool_is_blocked(db, monkeypatch):
    """analyst 编造 save_to_knowledge_base（写操作不在它的角色里）：
    执行前被拦下，工具没跑，子代理下一轮改用文字收尾。"""
    _enable_delegation(monkeypatch)
    knowledge = RecordingKnowledge()
    service, adapter = make_service(
        [
            {"tool_calls": [("delegate", {"role": "analyst", "task": "查预算"})]},
            {"tool_calls": [("save_to_knowledge_base", {"name": "x", "content": "y"})]},
            {"text": "总结报告"},
            {"text": "最终回答"},
        ],
        knowledge,
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    steps = [
        event
        for event in events
        if event["type"] == "agent_step" and event["phase"] == "tool_result"
    ]
    assert len(steps) == 1
    assert steps[0]["tool"] == "save_to_knowledge_base"
    assert steps[0]["status"] == "invalid_arguments"
    # 真实落库/写盘从未发生
    assert knowledge.uploaded == []
    # 主代理拿到的是子代理的报告，报告里不该出现越权工具名
    tool_messages = [
        message
        for message in adapter.calls[-1]["messages"]
        if message["role"] == "tool"
    ]
    assert "总结报告" in tool_messages[-1]["content"]
    assert "save_to_knowledge_base" not in tool_messages[-1]["content"]


def test_subagent_round_cap_truncates_to_report(db, monkeypatch):
    """analyst 最多 3 轮：前两轮可以调工具，第 3 轮被强制收报告并标记 truncated。"""
    _enable_delegation(monkeypatch)
    service, adapter = make_service(
        [
            {"tool_calls": [("delegate", {"role": "analyst", "task": "算两题"})]},
            {"tool_calls": [("calculate", {"expression": "1+1"})]},
            {"tool_calls": [("calculate", {"expression": "2+2"})]},
            {"text": "最终报告"},
            {"text": "好的"},
        ]
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    states = [event for event in events if event["type"] == "agent_state"]
    completed = next(event for event in states if event["status"] == "completed")
    assert completed["rounds"] == 3
    assert completed["truncated"] is True
    assert completed["steps"] == 2
    tool_messages = [
        message
        for message in adapter.calls[-1]["messages"]
        if message["role"] == "tool"
    ]
    assert "最终报告" in tool_messages[-1]["content"]


def test_subagent_shares_tool_result_budget_with_main(db, monkeypatch):
    """子代理吃掉的预算主代理看得见：报告过长被截断，预算耗尽后主代理收敛。

    2026-09-05 调过数字。原来是 ``total=70 / per_call=40``，而它成立**靠的是一个
    bug**：``_ToolResultBudget.take`` 在结果超出单次上限时把余额直接归零，
    而不是扣掉实际注入的那 40 个字符。于是 70 - 7(calculate) - 归零 = 0，
    预算"耗尽"，末轮不给 schema，断言通过。

    修掉归零之后余额是 70 - 7 - 40 = 23，预算没耗尽，schema 照给——测试红了，
    而红的原因和它要测的性质无关。所以把 total 调成 47：calculate 花 7、
    报告截断花 40，恰好归零，这次是**真的**耗尽。
    """
    _enable_delegation(monkeypatch)
    monkeypatch.setattr(settings, "TOOL_RESULT_TOTAL_CHARS", 47)
    monkeypatch.setattr(settings, "TOOL_RESULT_MAX_CHARS", 40)
    service, adapter = make_service(
        [
            {"tool_calls": [("delegate", {"role": "analyst", "task": "算 1+1"})]},
            {"tool_calls": [("calculate", {"expression": "1+1"})]},
            {"text": "这是一段足够长的分析报告，用来把剩余的工具结果预算吃掉大半。"},
            {"text": "基于已有信息作答"},
        ]
    )

    events = run(
        collect(service.stream_ai_response(db, "u1", "c1", "q", use_rag=True))
    )

    # 子代理的 calculate(7 字符)之后，委托报告超出剩余预算被截断
    tool_messages = [
        message
        for message in adapter.calls[3]["messages"]
        if message["role"] == "tool"
    ]
    assert len(tool_messages) == 1
    assert "[结果过长已截断" in tool_messages[0]["content"]
    # 预算耗尽 → 最后一轮不给 schema（没有工具调用），模型只能直接作答
    assert adapter.calls[-1]["tools"] == []
    assert any(event["type"] == "message_delta" for event in events)