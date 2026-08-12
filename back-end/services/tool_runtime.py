from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from services.model_adapter import ToolCall

logger = logging.getLogger("tool_runtime")


ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


class ToolStatus(str, Enum):
    """工具执行结果的分级,决定 Agent 循环该如何继续。"""

    OK = "ok"
    # 模型给错了工具名或参数:把错误回灌给模型,它下一轮可以自行修正。
    INVALID_ARGUMENTS = "invalid_arguments"
    # 工具自身或其依赖故障:重试同一调用无意义,应尽快收敛到最终回答。
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class ToolResult:
    """一次工具执行的结果。``content`` 始终是可直接回灌给模型的文本。"""

    content: str
    status: ToolStatus = ToolStatus.OK

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.OK


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def as_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRuntime:
    """执行经过校验的工具调用,工具 Schema 对提供商保持无关。"""

    def __init__(self, tools: list[ToolDefinition]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.as_openai_schema() for tool in self._tools.values()]

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if not tool:
            available = ", ".join(self._tools) or "无"
            return ToolResult(
                f"工具调用失败：未注册的工具 {call.name}。可用工具：{available}。",
                ToolStatus.INVALID_ARGUMENTS,
            )

        invalid = self._validate(tool, call.arguments)
        if invalid is not None:
            return ToolResult(invalid, ToolStatus.INVALID_ARGUMENTS)

        try:
            return ToolResult(await tool.handler(json.loads(call.arguments or "{}")))
        except Exception:
            # 不记录异常详情(可能含用户文本),仅记录工具名与类型由上层日志兜底。
            logger.exception("Tool %s failed", call.name)
            return ToolResult(
                f"工具调用失败：{call.name} 暂时不可用。请不要重试该工具，"
                f"直接基于已有信息回答。",
                ToolStatus.UNAVAILABLE,
            )

    @staticmethod
    def _validate(tool: ToolDefinition, raw_arguments: str) -> str | None:
        """按工具 Schema 校验参数,通过返回 None,否则返回给模型的错误说明。"""
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            return f"工具调用失败：{tool.name} 的参数不是合法 JSON。"
        if not isinstance(arguments, dict):
            return f"工具调用失败：{tool.name} 的参数必须是对象。"

        schema = tool.parameters
        missing = [name for name in schema.get("required", []) if name not in arguments]
        if missing:
            return f"工具调用失败：{tool.name} 缺少参数 {', '.join(missing)}。"

        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = [name for name in arguments if name not in properties]
            if extras:
                return f"工具调用失败：{tool.name} 包含未知参数 {', '.join(extras)}。"

        python_types: dict[str, Any] = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "object": dict,
            "array": list,
        }
        for name, value in arguments.items():
            expected_type = python_types.get(properties.get(name, {}).get("type"))
            # bool 是 int 的子类,integer/number 参数必须显式排除 True/False。
            if expected_type is None:
                continue
            if isinstance(value, bool) and expected_type in (int, (int, float)):
                return f"工具调用失败：{tool.name} 的参数 {name} 类型不正确。"
            if not isinstance(value, expected_type):
                return f"工具调用失败：{tool.name} 的参数 {name} 类型不正确。"
        return None
