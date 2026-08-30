"""子代理的轮次预算与"重复≠此路不通"这条区分。

来源是 2026-08-28 那次 Agent 评估:``supervisor`` 模式 toolRecall 从 0.812 掉到
0.438,三个失败全都是 ``web_search`` 一次都没调。报告里 ``web-vat-code`` 那条把
机制写得很清楚::

    calls = [delegate, list_knowledge_documents ×3]
    repeatedBlocked = 1
    expectTools = [web_search]
    toolRecall = 0.0
    stubQueries = []          # web_search 确实一次都没跑

推导出来的因果链:``researcher.max_rounds=4`` 而 ``AGENT_REPEAT_LIMIT=3``,于是

  第 1 轮  list_knowledge_documents(无参)  计数 1,放行
  第 2 轮  同一个调用                      计数 2,放行
  第 3 轮  同一个调用                      计数 3,**拦下** → barren
  ↑ barren == len(calls) → ``max_rounds = round_index + 1`` = 4
  第 4 轮  is_final → schemas 清空 → **再也调不到 web_search**

问题出在把 ``REPEATED`` 和 ``UNAVAILABLE`` 当成同一件事。``tool_runtime`` 里这两档
是刻意分开的(见 ``ToolStatus.REPEATED`` 的注释):前者是"这次调用多余,换个参数
仍然值得试",后者是"工具坏了,别再试"。而子代理的收敛逻辑对两者一样处置——
把工具全收走,恰好把"换个工具"这条唯一的出路也堵死了。
"""
from __future__ import annotations

from typing import Any

import pytest

from config import settings
from services import agent_roles, subagent
from services.model_adapter import ModelCompletion, ToolCall
from services.tool_runtime import (
    CircuitBreaker,
    ToolDefinition,
    ToolResult,
    ToolRuntime,
    ToolStatus,
)
from tests.conftest import run


class _RecordingAdapter:
    """按脚本回放子代理的每一轮，并记下每轮实际收到的工具名。"""

    def __init__(self, rounds: list[dict[str, Any]]) -> None:
        self._rounds = list(rounds)
        self.tools_seen: list[list[str]] = []

    async def complete(
        self, *, messages, tools, model="m", purpose="", **_kwargs
    ) -> ModelCompletion:
        self.tools_seen.append(
            [schema.get("function", {}).get("name") for schema in tools]
        )
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


def _runner(adapter, tools: list[ToolDefinition]) -> subagent.SubAgentRunner:
    return subagent.SubAgentRunner(
        adapter,
        ToolRuntime(tools, CircuitBreaker(0)),
        generation={"model": "m", "temperature": 0.0, "max_tokens": 512, "top_p": 1.0},
        take_budget=lambda text: text,
    )


@pytest.fixture(autouse=True)
def _repeat_limit(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_REPEAT_LIMIT", 3)


# ---- 复现 8/28 那次失败 ---------------------------------------------------


def test_重复调用之后仍应拿得到工具去换一条路(monkeypatch):
    """``web-vat-code`` 的真实形状。

    模型连发三次同一个无参调用,第三次被重复检测拦下。此时它**该**有机会改用
    ``web_search``——重复检测回灌的那句话就是"请改用不同的参数或别的工具"。
    把 schema 全收走等于给了建议又没收了执行它的手段。
    """
    role = agent_roles.get("researcher")
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("list_knowledge_documents", "{}")]},
            {"tool_calls": [("list_knowledge_documents", "{}")]},
            {"tool_calls": [("list_knowledge_documents", "{}")]},
            # 拿到"换个工具"的建议之后改用 web_search
            {"tool_calls": [("web_search", '{"query": "增值税税率"}')]},
            {"text": "报告：税率 13%，来源 …"},
        ]
    )
    tools = [
        _tool("list_knowledge_documents", "共 3 篇文档"),
        _tool("web_search", "搜索结果：13%"),
    ]
    outcome = run(_runner(adapter, tools).run(role, "查增值税税率"))

    called = [step.tool for step in outcome.steps]
    assert "web_search" in called, (
        f"重复检测拦下之后必须还能换工具，实际只调了 {called}"
    )
    assert not outcome.failed


def test_全是重复的一轮不该立刻收走工具(monkeypatch):
    """``REPEATED`` 与 ``UNAVAILABLE`` 的处置必须不同。

    前者是"这次调用多余，换个参数仍然值得试"，后者是"工具坏了，别再试"。
    ``tool_runtime`` 把它们分成两档正是因为处置不同，子代理这边不该又合并回去。
    """
    role = agent_roles.get("researcher")
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("list_knowledge_documents", "{}")]},
            {"tool_calls": [("list_knowledge_documents", "{}")]},
            {"tool_calls": [("list_knowledge_documents", "{}")]},
            {"tool_calls": [("web_search", '{"query": "x"}')]},
            {"text": "报告"},
        ]
    )
    tools = [_tool("list_knowledge_documents"), _tool("web_search")]
    run(_runner(adapter, tools).run(role, "任务"))

    # 被拦下那一轮之后的下一轮，仍应带着工具 schema
    blocked_round = 3  # 第 3 轮是被拦下的那一次（1-indexed）
    assert adapter.tools_seen[blocked_round], (
        "重复被拦下之后的一轮不该是空 schema——那正是它该换工具的时机"
    )


def test_工具真的不可用时才提前收敛():
    """与上面那条相对：``UNAVAILABLE`` 确实该尽快收敛。

    工具挂了就是挂了，把剩下的轮次全花在同一个失败上，等于把一次故障放大成
    一整个委派。
    """
    role = agent_roles.get("researcher")

    async def broken(_arguments):
        raise RuntimeError("backend down")

    tools = [
        ToolDefinition(
            name="web_search",
            description="web_search",
            parameters={"type": "object", "properties": {}},
            handler=broken,
        )
    ]
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("web_search", '{"query": "a"}')]},
            {"tool_calls": [("web_search", '{"query": "b"}')]},
            {"text": "报告：查不到，web_search 不可用。"},
        ]
    )
    outcome = run(_runner(adapter, tools).run(role, "任务"))
    # 不该把 5 轮全烧在一个坏工具上
    assert outcome.rounds <= 3, f"工具不可用时应尽快收敛，实际跑了 {outcome.rounds} 轮"


# ---- 轮次预算 -------------------------------------------------------------


def test_researcher的工具轮次够它跨两个来源():
    """researcher 是唯一横跨本地知识库与互联网的角色。

    最后一轮不下发工具（要它写报告），所以能用的工具轮数是 max_rounds - 1。
    它的提示词要求"两者都可能相关时先查本地再查网页"，那至少是:
    检索本地 → 列文档或读分块 → 转网页搜索 → 写报告。
    max_rounds=4 只给 3 个工具轮，转一次来源就用光了。
    """
    role = agent_roles.get("researcher")
    assert role.max_rounds - 1 >= 4, (
        f"researcher 只有 {role.max_rounds - 1} 个工具轮，跨本地与网页不够用"
    )


def test_子代理的重复上限比主代理紧():
    """主代理 6 轮里允许两次同样的调用，占三分之一；子代理轮次少得多。

    在 4~5 轮的预算里放过两次完全相同的调用，等于先烧掉一半再拦——而拦下来
    那一刻剩下的轮次已经不够换一条路了。这正是 8/28 那次失败的算术。
    """
    assert subagent.repeat_limit() < settings.AGENT_REPEAT_LIMIT


# ---- 空手回来的报告 -------------------------------------------------------


def test_一次工具都没调的报告要标成未核实(monkeypatch):
    """``memory-web`` 与 ``recovery-search-down`` 的形状:``calls=[delegate]``、
    ``repeatedBlocked=0``、``stubQueries=[]``——researcher 一次工具都没调就交了报告。

    这不是轮次问题,是它凭记忆答了。researcher 的职责是**找出处**,一份没有任何
    工具调用的报告在定义上就没有出处。最危险的是 ``recovery-search-down``:
    那道题考的恰恰是"查不到时如实说",而它 grounded 只有 1.0。

    主代理看不到检索过程,只看到报告正文。所以这件事必须由 ``format_report``
    显式标出来,而不是指望提示词每次都管用。
    """
    role = agent_roles.get("researcher")
    adapter = _RecordingAdapter([{"text": "增值税税率是 13%。"}])
    outcome = run(_runner(adapter, [_tool("web_search")]).run(role, "查增值税税率"))

    assert outcome.steps == []
    report = subagent.format_report(outcome)
    assert "未经检索" in report or "没有调用任何工具" in report, (
        f"空手回来的报告必须标出来，实际是：{report}"
    )


def test_有工具调用的报告不加那句警告():
    role = agent_roles.get("researcher")
    adapter = _RecordingAdapter(
        [
            {"tool_calls": [("web_search", '{"query": "x"}')]},
            {"text": "税率 13%，来源：example.com"},
        ]
    )
    outcome = run(_runner(adapter, [_tool("web_search")]).run(role, "任务"))
    report = subagent.format_report(outcome)
    assert "未经检索" not in report and "没有调用任何工具" not in report
