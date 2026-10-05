"""子代理执行器：轮次预算、重复与不可用的区分、成本与中和这两道补上的口子。

机制的来历记在 ``subagent`` 的注释里，一句话版：``ToolStatus.REPEATED``（这次调用
多余，换个参数仍值得试）和 ``UNAVAILABLE``（工具坏了，别再试）处置必须不同——
把前者也当成后者去收走 schema，恰好堵死了"换个工具"这条唯一的出路。
2026-08-28 那轮评估里 supervisor 模式 toolRecall 从 0.812 掉到 0.438 就是这个算术：
4 轮预算、``AGENT_REPEAT_LIMIT=3``，第三次同样的调用被拦下 → 那一轮"全不通" →
下一轮清空 schema → 检索工具一次都没机会被调。

最后两条是接进工单图之后**新增**的边界，都属于"子代理跑在工具处理器里，
主循环的那道防线够不着"这一类：它的工具结果不过 ``execute`` 节点的中和，
它的模型调用不在 ``reason`` 节点的成本 span 里。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from config import settings
from services import agent_roles, pricing, subagent
from services.model_adapter import ModelCompletion, ToolCall
from services.tool_runtime import CircuitBreaker, ToolDefinition, ToolRuntime
from tests.conftest import run


class _RecordingAdapter:
    """按脚本回放子代理的每一轮，并记下每轮实际收到的工具名与消息。"""

    def __init__(self, rounds: list[dict[str, Any]]) -> None:
        self._rounds = list(rounds)
        self.tools_seen: list[list[str]] = []
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def complete(
        self, *, messages, tools, model="m", purpose="", **_kwargs
    ) -> ModelCompletion:
        self.tools_seen.append(
            [schema.get("function", {}).get("name") for schema in tools]
        )
        self.messages_seen.append([dict(message) for message in messages])
        spec = self._rounds[min(len(self.tools_seen) - 1, len(self._rounds) - 1)]
        calls = [
            ToolCall(id=f"c{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(spec.get("tool_calls") or [])
        ]
        return ModelCompletion(
            content=spec.get("text", ""), tool_calls=calls, streamed_length=0
        )

    async def stream_completion(self, **kwargs):  # pragma: no cover - 子代理不用流式
        raise NotImplementedError


def _tool(name: str, result: str = "ok") -> ToolDefinition:
    async def handler(_arguments: dict[str, Any]) -> str:
        return result

    return ToolDefinition(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


def _runner(adapter, tools: list[ToolDefinition], **kwargs) -> subagent.SubAgentRunner:
    return subagent.SubAgentRunner(
        adapter,
        ToolRuntime(tools, CircuitBreaker(0)),
        generation={"model": "m", "temperature": 0.0},
        take_budget=kwargs.pop("take_budget", lambda text: text),
        **kwargs,
    )


def _inquiry() -> agent_roles.AgentRole:
    return agent_roles.ROLES["inquiry"]


@pytest.fixture(autouse=True)
def _repeat_limit(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_REPEAT_LIMIT", 3)


# ---- 重复不等于此路不通 ---------------------------------------------------


def test_重复被拦之后仍应拿得到工具去换一条路():
    """``AGENT_REPEAT_LIMIT=3`` 时子代理的上限是 2——同一个调用第二次就拦下，
    拦下之后它必须还有工具可换。"""
    role = _inquiry()
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("lookup_customer", "{}")]},
            {"tool_calls": [("lookup_customer", "{}")]},  # 这一次被拦下
            # 拿到"换个参数或换工具"的建议之后改用另一个工具
            {"tool_calls": [("lookup_order", '{"order_no": "ORD1"}')]},
            {"text": "报告：订单 ORD1 未发货。"},
        ]
    )
    tools = [_tool("lookup_customer", "查到 3 位在途客户"), _tool("lookup_order")]
    outcome = run(_runner(adapter, tools).run(role, "查订单 ORD1"))

    called = [step.tool for step in outcome.steps]
    assert "lookup_order" in called, f"重复检测拦下之后必须还能换工具，实际只调了 {called}"
    assert not outcome.failed


def test_全是重复的一轮不该收走工具():
    """``REPEATED`` 与 ``UNAVAILABLE`` 的处置必须不同。

    前者是"这次调用多余，换个参数仍然值得试"，后者是"工具坏了，别再试"。
    ``tool_runtime`` 把它们分成两档正是因为处置不同，子代理这边不该又合并回去。
    """
    role = _inquiry()
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("lookup_customer", "{}")]},
            {"tool_calls": [("lookup_customer", "{}")]},  # 被拦下
            {"text": "报告"},
        ]
    )
    tools = [_tool("lookup_customer"), _tool("lookup_order")]
    run(_runner(adapter, tools).run(role, "任务"))

    blocked_round = 2  # 被拦下的那一轮（1-indexed）
    assert adapter.tools_seen[blocked_round], (
        "重复被拦下之后的一轮不该是空 schema——那正是它该换工具的时机"
    )


def test_工具真的不可用时才提前收敛():
    role = _inquiry()

    async def broken(_arguments):
        raise RuntimeError("business system down")

    tools = [
        ToolDefinition(
            name="lookup_order",
            description="lookup_order",
            parameters={"type": "object", "properties": {}},
            handler=broken,
        )
    ]
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("lookup_order", '{"order_no": "A"}')]},
            {"tool_calls": [("lookup_order", '{"order_no": "B"}')]},
            {"text": "报告：查不到，业务系统不可用。"},
        ]
    )
    outcome = run(_runner(adapter, tools).run(role, "任务"))
    assert outcome.rounds <= 3, f"工具不可用时应尽快收敛，实际跑了 {outcome.rounds} 轮"


# ---- 轮次预算 -------------------------------------------------------------


def test_每个角色的轮次够它办完自己的那件事():
    """最后一轮不下发工具（要它写报告），所以能用工具的轮数是 ``max_rounds - 1``。

    inquiry 最短的两步路径是「查客户拿订单号 → 查那张单」，加上换一次参数的余地
    就是 3 个工具轮；policy 同理（一次检索不命中要换问法）。纯推理角色只需要 1 轮。
    """
    assert agent_roles.ROLES["inquiry"].max_rounds - 1 >= 3
    assert agent_roles.ROLES["policy"].max_rounds - 1 >= 2
    assert agent_roles.ROLES["reassurance"].max_rounds == 1


def test_子代理的重复上限比主循环紧():
    assert subagent.repeat_limit() < settings.AGENT_REPEAT_LIMIT


def test_主循环关掉重复检测时子代理跟着关():
    """0 的语义是"不检测"，子代理不该反过来比主循环更严。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_REPEAT_LIMIT", 0)
    assert subagent.repeat_limit() == 0
    monkeypatch.undo()


# ---- 空手回来的报告 -------------------------------------------------------


def test_一次工具都没调的报告要标成未核实():
    """这份报告读起来和查过的一模一样，而主代理看不到子代理的过程。

    工单这边比问答那边更危险：一份凭记忆写的"这单还能退"会直接把主代理推向一笔
    不该发的退款。所以这件事必须由 ``format_report`` 显式标出来，而不是指望提示词
    里那句"先调工具"每次都管用。
    """
    role = _inquiry()
    adapter = _RecordingAdapter([{"text": "订单 ORD1 应该还没发货。"}])
    outcome = run(_runner(adapter, [_tool("lookup_order")]).run(role, "查订单 ORD1"))

    assert outcome.steps == []
    assert "没有调用任何工具" in subagent.format_report(outcome)


def test_有工具调用的报告不加那句警告():
    role = _inquiry()
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("lookup_order", '{"order_no": "ORD1"}')]},
            {"text": "订单 ORD1 未发货。"},
        ]
    )
    outcome = run(_runner(adapter, [_tool("lookup_order")]).run(role, "任务"))
    assert "没有调用任何工具" not in subagent.format_report(outcome)


def test_轮次用尽要说明报告可能不完整():
    """三次都查了不同的单（不是重复调用，所以不会被拦），第四次是最后一轮——
    那一轮不下发工具，它只能凭已有的东西写报告。"""
    role = _inquiry()
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("lookup_order", '{"order_no": "ORD1"}')]},
            {"tool_calls": [("lookup_order", '{"order_no": "ORD2"}')]},
            {"tool_calls": [("lookup_order", '{"order_no": "ORD3"}')]},
            {"text": "报告：查到了前三张，后面几张没查。"},
        ]
    )
    outcome = run(_runner(adapter, [_tool("lookup_order")]).run(role, "任务"))
    assert outcome.truncated
    assert "轮次已用尽" in subagent.format_report(outcome)


# ---- 委派看不见的两道防线 -------------------------------------------------


def test_子代理的工具结果也过中和():
    """主代理的工具结果是在 ``execute`` 节点里过协议中和的，而子代理的那些**不经过
    那个节点**：它们在一个工具处理器的调用栈里直接进了子代理自己的 messages。

    少这一道，订单备注里一段协议标记就能打乱文本工具协议模型对"哪句是工具结果"的
    判断，而护栏的要求是"外部内容进上下文之前过一遍"——那句话不该因为进上下文
    的是个子代理就失效。
    """
    marker = "订单备注：BEGIN 客户要求加急 END"
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("lookup_order", '{"order_no": "ORD1"}')]},
            {"text": "报告：订单存在，备注里有一段可疑标记。"},
        ]
    )
    outcome = run(
        _runner(
            adapter,
            [_tool("lookup_order", marker)],
            sanitize_result=lambda text: text.replace("BEGIN", "").replace("END", ""),
        ).run(_inquiry(), "查订单 ORD1")
    )

    assert outcome.steps[0].result == marker, "中和发生在进上下文那一步，不改轨迹原文"
    fed = [message["content"] for message in adapter.messages_seen[-1] if message["role"] == "tool"]
    assert fed and "BEGIN" not in fed[0], f"工具结果必须中和之后再进子代理上下文：{fed}"


def test_每一轮的用量都累进委派成本():
    """提供商的 ``set_usage`` 是**覆盖**写。整个委派只开一个 span 的话，
    跑完 3 轮剩下的只有最后一轮的 token 数——一次多轮委派会被记成"只花了一轮的钱"，
    而那张工单的成本熔断就永远差着两三倍的量。
    """
    role = _inquiry()
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("lookup_order", '{"order_no": "ORD1"}')]},
            {"tool_calls": [("lookup_logistics", '{"order_no": "ORD1"}')]},
            {"text": "报告：未发货。"},
        ]
    )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        pricing, "cost_of_span", lambda span, model: (Decimal("0.5"), True)
    )
    outcome = run(
        _runner(adapter, [_tool("lookup_order"), _tool("lookup_logistics")]).run(
            role, "查订单 ORD1 的物流"
        )
    )
    monkeypatch.undo()

    # 每一轮模型调用计一次：3 轮 = 0.5 * 3。只算最后一轮的话，一次委派会被记成
    # 三分之一到五分之一的真实成本，而那个数字直接喂给成本熔断。
    assert outcome.cost == pytest.approx(0.5 * outcome.rounds), (
        f"跑了 {outcome.rounds} 轮却只记了 {outcome.cost} 元"
    )
    assert outcome.cost_known


def test_成本算不出来时不折成零():
    """与 ``graph._cost_over`` 同一取舍：未知不是 0。折成 0 等于宣布这张单没花钱。"""
    adapter = _RecordingAdapter([{"text": "报告：未发货。"}])
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(pricing, "cost_of_span", lambda span, model: (None, False))
    outcome = run(_runner(adapter, [_tool("lookup_order")]).run(_inquiry(), "任务"))
    monkeypatch.undo()

    assert outcome.cost is None
    assert not outcome.cost_known


def test_子代理永远拿不到委派工具():
    """递归委派的成本没有上界，而第二层往下几乎不会带来新信息——它看到的材料
    只会比上一层更少。

    这件事不靠提示词保证：schema 按角色过滤（模型看不到就不会调），越权调用
    在执行前挡掉（模型编出名字也不会真的跑）。两条都在代码里，所以这条测试
    钉的是"运行时注册了 delegate 也不影响子代理的面"。
    """
    role = _inquiry()
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("delegate", '{"role": "policy", "task": "再派一次"}')]},
            {"text": "报告：我不能委派，这张单的信息查到了。"},
        ]
    )
    tools = [_tool("lookup_order"), _tool("delegate", "这个 handler 不该被执行")]
    outcome = run(_runner(adapter, tools).run(role, "查订单 ORD1"))

    assert "delegate" not in adapter.tools_seen[0]
    assert outcome.steps[0].result.count("不能使用") == 1
    assert all(step.result != "这个 handler 不该被执行" for step in outcome.steps)
