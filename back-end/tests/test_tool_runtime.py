"""ToolRuntime 的参数校验与错误分级。

分级的意义在于：参数错是"模型的错"，回灌后它能自行修正；工具故障是环境问题，
重试同一调用无意义，Agent 循环应尽快收敛。两者不能混为一谈。
"""
from __future__ import annotations

from typing import Any

from conftest import run
from services.model_adapter import ToolCall
from services.tool_runtime import ToolDefinition, ToolRuntime, ToolStatus


def _runtime(handler=None) -> ToolRuntime:
    async def default_handler(arguments: dict[str, Any]) -> str:
        return f"ok:{arguments}"

    return ToolRuntime(
        [
            ToolDefinition(
                name="demo",
                description="demo tool",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                handler=handler or default_handler,
            )
        ]
    )


def _call(name: str = "demo", arguments: str = "{}") -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


def test_valid_call_returns_ok():
    result = run(_runtime().execute(_call(arguments='{"text": "hi"}')))

    assert result.status is ToolStatus.OK
    assert result.ok


def test_unknown_tool_reports_available_names():
    result = run(_runtime().execute(_call(name="nope")))

    assert result.status is ToolStatus.INVALID_ARGUMENTS
    assert "未注册的工具" in result.content
    assert "demo" in result.content


def test_malformed_json_arguments():
    result = run(_runtime().execute(_call(arguments="{not json")))

    assert result.status is ToolStatus.INVALID_ARGUMENTS
    assert "合法 JSON" in result.content


def test_missing_required_argument():
    result = run(_runtime().execute(_call(arguments='{"count": 1}')))

    assert result.status is ToolStatus.INVALID_ARGUMENTS
    assert "缺少参数 text" in result.content


def test_unknown_argument_rejected():
    result = run(_runtime().execute(_call(arguments='{"text": "hi", "extra": 1}')))

    assert result.status is ToolStatus.INVALID_ARGUMENTS
    assert "未知参数 extra" in result.content


def test_wrong_type_rejected():
    result = run(_runtime().execute(_call(arguments='{"text": 1}')))

    assert result.status is ToolStatus.INVALID_ARGUMENTS
    assert "类型不正确" in result.content


def test_bool_is_not_accepted_as_integer():
    """bool 是 int 的子类，不显式排除的话 true 会被当成合法 integer。"""
    result = run(_runtime().execute(_call(arguments='{"text": "hi", "count": true}')))

    assert result.status is ToolStatus.INVALID_ARGUMENTS


def test_handler_exception_maps_to_unavailable():
    async def boom(_arguments: dict[str, Any]) -> str:
        raise RuntimeError("db down")

    result = run(_runtime(handler=boom).execute(_call(arguments='{"text": "hi"}')))

    assert result.status is ToolStatus.UNAVAILABLE
    assert "暂时不可用" in result.content


def test_工具失败时明确禁止用记忆里的数字顶替():
    """失败消息不能只说"基于已有信息回答"——那在邀请模型编。

    起因是评估里 recovery-search-down：web_search 故障之后模型编了一个汇率
    （"通常在 6.3-6.8 之间，假设 6.5"）再拿它算出 6500。当那次检索**就是唯一的
    信息来源**时，"直接基于已有信息回答"读起来就是"那你直接答吧"。

    这条钉住的是"撤掉那句邀请"，不是某个具体措辞：断言的是禁止编造这个语义在场。
    """
    async def boom(_arguments: dict[str, Any]) -> str:
        raise RuntimeError("search down")

    result = run(_runtime(handler=boom).execute(_call(arguments='{"text": "hi"}')))

    assert result.status is ToolStatus.UNAVAILABLE
    assert "不要用记忆里的数字" in result.content
    # 原来那句话还留着（它本身没错，错在只有它）
    assert "基于已有信息" in result.content
