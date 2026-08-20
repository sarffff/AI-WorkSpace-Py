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
    # 同一个 (工具, 参数) 已经执行过足够多次,这次没有真的执行。
    # 与 UNAVAILABLE 分开是因为处置不同:工具本身好着,是这次调用多余,
    # 换个参数仍然值得试——所以纠正说明里要请它改参数,而不是请它别再用这个工具。
    REPEATED = "repeated"
    # 人工审批被拒绝,这次没有执行。与上面三档都不同:工具好着、参数可能也对、
    # 更不是重复调用——是**人不同意**。处置也不同:不要重试,去问用户想怎么改。
    # 单独一档而不是复用 UNAVAILABLE,是因为它绝不该触发熔断:用户拒绝一次不代表
    # 这个工具坏了,把它熔断掉会让用户改主意之后反而调不动。
    REJECTED = "rejected"


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


class RepeatGuard:
    """同一 (工具, 参数) 的重复执行检测。

    轮次上限和结果字符预算都管不到这件事:重复调用每次都是合法调用、都在预算内,
    只是拿回来的东西一模一样。表现是模型用几乎相同的查询连发三轮,把六轮预算烧掉
    四轮,然后基于同一份材料作答——每一步单看都正常。

    三个设计决定:

    1. **算总次数,不算"连续三次"。** A、B、A、B、A 这种在两个相同查询之间来回摆
       的情况是同一种病,只看连续完全抓不到。代价是跨轮次的合理重查(检索完读了
       文档、想再用同一个词检索一次)也会被算进去——但那种重查本来也拿不到新东西。
    2. **参数按 key 排序后序列化。** ``{"a":1,"b":2}`` 和 ``{"b":2,"a":1}`` 是同一次
       调用,不排序会让一半的重复漏掉。
    3. **拦下来不等于静默丢弃。** 第 N 次起返回一句纠正说明当作工具结果回灌,
       模型于是知道"这条路走过了,换个说法或者直接作答"。静默返回上次的结果会让
       它以为这次调用成功了,下一轮接着调。
    """

    __slots__ = ("_limit", "_counts", "blocked")

    def __init__(self, limit: int) -> None:
        # 0 或负数表示关闭检测,退回改动前的行为
        self._limit = max(0, limit)
        self._counts: dict[tuple[str, str], int] = {}
        # 累计拦下了几次。进埋点用——"这次回答里模型原地转了几圈"是可查询的事实,
        # 而不是要靠翻日志才能发现的东西。
        self.blocked = 0

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    @staticmethod
    def key(name: str, arguments: Any) -> tuple[str, str]:
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            encoded = str(arguments)
        return name, encoded

    def snapshot(self) -> tuple[dict[str, int], int]:
        """把计数导出成可 JSON 序列化的形状。

        键是 ``(name, arguments)`` 元组，JSON 没有元组，所以拼成一个字符串。
        分隔符用 ``\\x00``：工具名和序列化后的参数都不可能包含它，
        换成 ``:`` 或 ``|`` 都可能撞上参数里的字符，还原时就切错了。
        """
        return (
            {f"{name}\x00{args}": count for (name, args), count in self._counts.items()},
            self.blocked,
        )

    def restore(self, counts: dict[str, int], blocked: int) -> None:
        """从快照恢复计数。

        必须恢复，否则"中断一次"就等于"把重复检测重置一次"：用户点一下同意，
        模型又能拿同样的参数再检索三遍。
        """
        self._counts = {}
        for key, count in (counts or {}).items():
            name, _, args = key.partition("\x00")
            self._counts[(name, args)] = int(count)
        self.blocked = int(blocked or 0)

    def check(self, name: str, arguments: Any) -> ToolResult | None:
        """登记这次调用。允许执行返回 None,该拦时返回要回灌的结果。

        计数在**判定之后**才加,所以 limit=3 的语义是"前两次照跑,第三次起拦下"。
        被拦的调用也计数:模型无视纠正继续调时,次数应该接着涨,这样埋点里
        ``repeated_blocked`` 反映的是真实的浪费次数。
        """
        if not self.enabled:
            return None
        identity = self.key(name, arguments)
        seen = self._counts.get(identity, 0)
        self._counts[identity] = seen + 1
        if seen + 1 < self._limit:
            return None
        self.blocked += 1
        return ToolResult(
            f"工具调用已被拦截：{name} 带着完全相同的参数，这是第 {seen + 1} 次调用"
            f"（本次回答的上限是 {self._limit} 次），再执行一次不会得到新的内容。"
            "请改用不同的参数（换检索词、换文档、换范围），"
            "或者基于已经拿到的信息直接作答。",
            ToolStatus.REPEATED,
        )


class CircuitBreaker:
    """单工具连续失败熔断。

    轮次上限收敛的是"模型用光预算"，而它针对的是"同一个工具反复失败"——
    那是幻觉（参数总写不对）或通道故障（工具后端挂了）的信号。让模型在剩余
    轮次里继续试同一个工具，每一次都拿回同样的失败，等于把失败从一次扩散成
    一整个回合。

    三个设计决定：

    1. **连续失败才熔断，偶尔一次不算。** 模型偶尔把参数写错很常见，改正后
       立刻重置计数。只有连续 N 次失败才说明这个工具当下不值得再试。
    2. **熔断 = 移除 schema，而不是拒绝执行。** 模型看不到被熔断的工具就不会
       发起调用，比"每轮都试一次、每次都拿回一句拒绝"省轮次；而``schemas``
       之外再留一道执行前拦截，是防"消息里还挂着旧的工具调用"的兜底。
    3. **作用域是单次回答。** 每次回答都新建 ToolRuntime（也就新建熔断器）：
       上一回合失败的通道这一回合可能已经恢复，跨回合熔断会把一次偶发故障
       变成之后每一轮都调不到这个工具。
    """

    __slots__ = ("_limit", "_consecutive", "tripped")

    def __init__(self, limit: int) -> None:
        # 0 或负数表示关闭,退回改动前的行为
        self._limit = max(0, limit)
        self._consecutive: dict[str, int] = {}
        # 已熔断的工具名。熔断器自己写，``schemas`` 与 ``execute`` 读。
        self.tripped: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def snapshot(self) -> tuple[dict[str, int], list[str]]:
        return dict(self._consecutive), sorted(self.tripped)

    def restore(self, consecutive: dict[str, int], tripped: list[str]) -> None:
        """从快照恢复熔断状态。

        不恢复的话，一次中断就能让被熔断的工具复活——而它上一秒还在连续失败。
        用户点"同意"的语义是"这一次写操作我批准了"，不是"把本轮所有熔断都清掉"。
        """
        self._consecutive = {
            str(name): int(count) for name, count in (consecutive or {}).items()
        }
        self.tripped = set(tripped or [])

    def note(self, name: str, status: ToolStatus) -> None:
        """登记一次工具执行的结局。成功清零，连续失败达到阈值即熔断。"""
        if not self.enabled:
            return
        if status is ToolStatus.OK:
            self._consecutive.pop(name, None)
            self.tripped.discard(name)
            return
        count = self._consecutive.get(name, 0) + 1
        self._consecutive[name] = count
        if count >= self._limit:
            self.tripped.add(name)


class ToolRuntime:
    """执行经过校验的工具调用,工具 Schema 对提供商保持无关。"""

    def __init__(
        self,
        tools: list[ToolDefinition],
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._breaker = breaker or CircuitBreaker(0)

    @property
    def schemas(self) -> list[dict[str, Any]]:
        # 被熔断的工具不再下发：模型看不到它，就不会再发起调用。
        return [
            tool.as_openai_schema()
            for tool in self._tools.values()
            if tool.name not in self._breaker.tripped
        ]

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

        if call.name in self._breaker.tripped:
            # 模型已经看不到这个工具了，会走到这里是因为消息里还挂着熔断前
            # 发出的调用。拦下并说明，而不是让它真的跑一遍。
            return ToolResult(
                f"工具调用失败：{call.name} 在本轮已因连续失败被熔断，不再执行。"
                "请换用其他工具，或基于已有信息直接回答。",
                ToolStatus.UNAVAILABLE,
            )

        invalid = self._validate(tool, call.arguments)
        if invalid is not None:
            self._breaker.note(call.name, ToolStatus.INVALID_ARGUMENTS)
            return ToolResult(invalid, ToolStatus.INVALID_ARGUMENTS)

        try:
            result = await tool.handler(json.loads(call.arguments or "{}"))
            self._breaker.note(call.name, ToolStatus.OK)
            return ToolResult(result)
        except Exception:
            # 不记录异常详情(可能含用户文本),仅记录工具名与类型由上层日志兜底。
            logger.exception("Tool %s failed", call.name)
            self._breaker.note(call.name, ToolStatus.UNAVAILABLE)
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
