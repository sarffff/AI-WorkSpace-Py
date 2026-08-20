"""子代理执行器：跑完一次委派并把报告交回主代理。

这是 ``chat_service._run_turn`` 的一个精简版，刻意不复用它：主循环还要处理对话
历史、滚动摘要、RAG 预检索、语义缓存、护栏事件、最终回答的流式透出。子代理一样
都不需要——它拿到的是**一句任务描述**，不是一段对话；它的产出是给主代理读的报告，
不流给用户看。把这些分支都塞进同一个函数，会让每个分支都得先判断"我现在是主代理
还是子代理"。

三个关键约束：

1. **子代理看不到对话历史。** 只给它任务描述。这不是省事,是委派的全部意义所在:
   如果它还要读完整段对话,那主代理直接自己做就行了,委派只多付了一次生成。
   代价是任务描述写得不好它就查错方向——那正是主代理该负的责任。
2. **预算是共享的。** ``budget`` 由主循环传进来,子代理消耗的是同一份总额。
   各自独立计预算的话,委派三次就等于把上下文预算用了四倍,而这件事在
   任何单独一次调用里都看不出来。
3. **不能再委派。** 子代理的工具面里永远没有 ``delegate``:递归委派的成本没有
   上界,而且第二层往下几乎不会带来新信息——它看到的材料只会比上一层更少。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from config import settings
from services import agent_roles, guardrails, prompt_library
from services.agent_roles import AgentRole
from services.model_adapter import ModelAdapter, ModelCompletion
from services.telemetry import SpanKind, tracer
from services.tool_runtime import RepeatGuard, ToolRuntime, ToolStatus

logger = logging.getLogger("subagent")


@dataclass(slots=True)
class SubAgentStep:
    """子代理执行的一步工具调用。交回主循环去发 SSE、去落库。

    子代理自己不发事件也不写库:它是在一个工具处理器内部跑的,那里既拿不到
    SSE 的生成器,也不该替主循环决定轨迹怎么归属。
    """

    round_index: int
    call_index: int
    tool: str
    status: str
    arguments: dict[str, Any]
    result: str
    tool_call_id: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class SubAgentOutcome:
    """一次委派的结果。"""

    role: str
    report: str
    steps: list[SubAgentStep]
    rounds: int
    # 轮次用尽而不是自己收敛的。主代理该知道这份报告可能是半截的——
    # 它读到的文字不会说"我还没查完",看起来和查完了一模一样。
    truncated: bool = False
    failed: bool = False


def role_prompt(role: AgentRole) -> str:
    return prompt_library.get(role.prompt_key).render()


class SubAgentRunner:
    """按角色跑一次委派。

    ``take_budget`` 是主循环的 ``_ToolResultBudget.take``:子代理的工具结果和
    主代理的走同一份预算,签名只暴露一个函数而不是整个预算对象,是为了让
    "子代理只能花钱、不能查还剩多少、也不能改" 这件事由类型保证。
    """

    def __init__(
        self,
        model_adapter: ModelAdapter,
        runtime: ToolRuntime,
        *,
        generation: dict[str, Any],
        take_budget: Callable[[str], str],
    ) -> None:
        self._adapter = model_adapter
        self._runtime = runtime
        self._generation = generation
        self._take_budget = take_budget

    def _schemas_for(self, role: AgentRole) -> list[dict[str, Any]]:
        """该角色能用的工具 schema。

        取交集而不是照 ``role.tools`` 全给:知识库开关关着的时候
        ``search_knowledge_base`` 根本没注册,给了它 schema 就是让它去调一个
        不存在的工具,白烧一轮。
        """
        allowed = set(role.tools)
        return [
            schema
            for schema in self._runtime.schemas
            if schema.get("function", {}).get("name") in allowed
        ]

    async def run(self, role: AgentRole, task: str) -> SubAgentOutcome:
        """执行委派。任何异常都收敛成 ``failed`` 的结果,不往主循环抛。

        子代理挂掉不该让整个回答挂掉:主代理拿到一句"这次委派失败了"之后
        完全可以自己去做,或者告诉用户这部分没查到。
        """
        steps: list[SubAgentStep] = []
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": role_prompt(role)},
            {"role": "user", "content": task},
        ]
        # 重复检测的作用域是**这一次委派**,不和主代理共享。
        #
        # 共享会立刻出错:子代理看不到对话历史(见模块文档的约束 1),所以它去查
        # 主代理刚查过的东西是完全正当的——那是它拿到任务后的第一次调用,不是重复。
        # 共享计数会把这第一次就算成第二次,委派两次之后 researcher 一句都查不动。
        repeats = RepeatGuard(settings.AGENT_REPEAT_LIMIT)
        max_rounds = max(1, role.max_rounds)
        report_parts: list[str] = []
        round_index = 0
        truncated = False

        async with tracer.span(
            f"agent.{role.name}",
            SpanKind.AGENT,
            role=role.name,
            task_chars=len(task),
            max_rounds=max_rounds,
        ) as span:
            while True:
                round_index += 1
                is_final = round_index >= max_rounds
                schemas = [] if is_final else self._schemas_for(role)
                if is_final and round_index > 1:
                    truncated = True
                    messages.append(
                        {
                            "role": "user",
                            "content": "[系统提示] 工具调用阶段已结束，请基于目前已获得的"
                            "信息给出报告，并在报告里说明哪些部分尚未查证。",
                        }
                    )

                completion = await self._complete(messages, schemas, role)
                if completion is None:
                    span.set(failed=True)
                    return SubAgentOutcome(
                        role=role.name,
                        report="",
                        steps=steps,
                        rounds=round_index,
                        failed=True,
                    )

                if completion.protocol_error:
                    span.set(protocol_error=True)
                    return SubAgentOutcome(
                        role=role.name,
                        report=completion.content or "",
                        steps=steps,
                        rounds=round_index,
                        failed=not completion.content.strip(),
                    )

                calls = [] if is_final else completion.tool_calls
                if not calls:
                    if completion.content.strip():
                        report_parts.append(completion.content)
                    break

                messages.append(completion.as_assistant_message())
                text_results: list[str] = []
                barren = 0

                for call_index, call in enumerate(calls):
                    try:
                        arguments = json.loads(call.arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}

                    # 越权调用在这里挡掉,而不是靠提示词劝它别调:schema 是按角色
                    # 过滤过的,能走到这里说明模型自己编了个工具名。回灌一句说明
                    # 让它下一轮改,比直接执行要安全——researcher 拿到写操作
                    # 就是把"需要用户确认"这条约束绕过去了。
                    if call.name not in set(role.tools):
                        result_text = (
                            f"工具调用失败：{role.name} 不能使用 {call.name}。"
                            f"你可用的工具：{', '.join(role.tools) or '无'}。"
                        )
                        status = ToolStatus.INVALID_ARGUMENTS.value
                        step_citations: list[dict[str, Any]] = []
                    else:
                        repeated = repeats.check(call.name, arguments)
                        if repeated is not None:
                            # 子代理轮次比主代理少(role.max_rounds),原地转圈的代价
                            # 相对更高:三轮里浪费一轮就是三分之一。
                            result_text = repeated.content
                            status = repeated.status.value
                            barren += 1
                        else:
                            with guardrails.collecting():
                                # 护栏命中记在外层 delegate 那一步:子代理跑在
                                # 主循环的 collecting 作用域里,不隔一层的话
                                # 同一次命中会被两边各收一遍。
                                result = await self._runtime.execute(call)
                            result_text = result.content
                            status = result.status.value
                            if result.status is ToolStatus.UNAVAILABLE:
                                barren += 1
                        step_citations = []

                    steps.append(
                        SubAgentStep(
                            round_index=round_index,
                            call_index=call_index,
                            tool=call.name,
                            status=status,
                            arguments=arguments,
                            result=result_text,
                            tool_call_id=call.id,
                            citations=step_citations,
                        )
                    )

                    content = self._take_budget(result_text)
                    if completion.uses_text_tool_protocol:
                        text_results.append(f"工具 {call.name} 的结果：\n{content}")
                    else:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": content,
                            }
                        )

                if completion.uses_text_tool_protocol:
                    messages.append(
                        {
                            "role": "user",
                            "content": "以下是已执行工具的内部结果。请据此继续，"
                            "必要时可再次调用工具；不要展示工具调用标记。\n\n"
                            + "\n\n".join(text_results),
                        }
                    )

                if barren == len(calls):
                    # 本轮没有一次调用带回新东西(全不可用,或全是重复调用),
                    # 继续只会原地转圈。下一轮不给 schema,逼它用已有信息写报告。
                    max_rounds = round_index + 1

            report = "\n\n".join(part for part in report_parts if part.strip()).strip()
            span.set(
                rounds=round_index,
                steps=len(steps),
                report_chars=len(report) or None,
                truncated=truncated or None,
                repeated_blocked=repeats.blocked or None,
            )
            return SubAgentOutcome(
                role=role.name,
                report=report,
                steps=steps,
                rounds=round_index,
                truncated=truncated,
                failed=not report,
            )

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        schemas: list[dict[str, Any]],
        role: AgentRole,
    ) -> ModelCompletion | None:
        """子代理只用非流式调用。

        它的输出不透给用户,流式带来的唯一好处(首字延迟)在这里没有意义,
        而非流式少一层增量装配。
        """
        try:
            return await self._adapter.complete(
                messages=messages,
                tools=schemas,
                purpose=f"subagent.{role.name}",
                **self._generation,
            )
        except Exception as exc:
            logger.error(
                "subagent %s completion failed: %s", role.name, type(exc).__name__
            )
            return None


def build_delegate_schema(roles: list[AgentRole]) -> dict[str, Any]:
    """``delegate`` 工具的 JSON Schema。

    ``role`` 用 enum 而不是自由字符串:角色名写错时 enum 让提供商侧就拦下来,
    否则要等执行阶段回灌一句"没有这个角色",白付一轮。

    描述里把每个角色能干什么列全,因为这是主代理选人的唯一依据——工具描述
    是它能看到的全部信息,角色的提示词它看不到。

    **这里只写契约,不写策略。** "什么时候值得委派"归系统提示词
    (``chat_system_rag`` 的 v5-augment / v6-supervisor),原因有两个:

    1. 提示词是版本化的、进 A/B 的、写进 trace 的 ``prompt_version``;这段
       description 不是。同一件事在两处各写一遍,改了一处忘另一处时模型会同时
       收到两份矛盾的策略,而 trace 只记得其中一份。
    2. 策略本身**因模式而异**。原来这里写着"能直接调工具解决的事自己做",
       在 supervisor 模式下是错的——那时主代理没有那些工具。一段固定的
       description 说不清一件随配置变化的事。

    留在这里的是不随模式变的事实:子代理看不到对话(所以 task 必须自包含)、
    有哪些角色、各自能做什么。
    """
    lines = [
        "把一个独立的子任务交给专门的子代理执行，并拿回它的报告。",
        "子代理看不到本次对话，只能看到你在 task 里写的内容。",
        "可用的子代理：",
    ]
    lines += [f"- {role.name}：{role.summary}" for role in roles]
    return {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "enum": [role.name for role in roles],
                "description": "要委派给哪个子代理",
            },
            "task": {
                "type": "string",
                # 这是整个工具面里唯一一个"写法质量直接决定成败"的参数:其它工具
                # 的参数都是单值(查询词、路径、表达式),写错了模型能从报错里看出来,
                # 而一句写得含糊的 task 会拿回一份看起来很正常但查错方向的报告。
                # 所以这里给一正一反两个例子——提示词里反复讲"必须自包含"是抽象的,
                # 一个反例比三句叮嘱更能说明"含糊"长什么样。
                "description": (
                    "给子代理的任务描述。它看不到对话历史，所以背景、要查什么、"
                    "你已经知道的相关信息、需要它核对的材料原文，都要写进这一段里。\n"
                    "好的例子：「查明公司差旅住宿费的一线城市每晚上限。用户问的是"
                    "上季度出差 7 晚的报销总额，我已知市内交通为 540 元。"
                    "请给出上限金额及其出处（文档名与分块号）。」\n"
                    "不好的例子：「帮我查一下这个的标准是多少」——子代理不知道"
                    "「这个」指什么、不知道要查哪一类标准，只能瞎查。"
                ),
            },
        },
        "required": ["role", "task"],
        "additionalProperties": False,
    }, "\n".join(lines)


def format_report(outcome: SubAgentOutcome) -> str:
    """把子代理的报告包成回灌给主代理的文本。

    标注是"子代理的报告"而不是直接贴正文:主代理必须知道这段话不是它自己
    查到的,里面的事实需要按报告里给的出处来对待。截断和失败也要显式说明——
    半截的报告读起来和完整的一样自然。
    """
    if outcome.failed and not outcome.report:
        return (
            f"委派给 {outcome.role} 失败，没有拿到报告。"
            f"请自己处理这部分，或者告诉用户这部分未能完成。"
        )
    header = f"[来自 {outcome.role} 子代理的报告，共 {outcome.rounds} 轮"
    if outcome.steps:
        header += f"、{len(outcome.steps)} 次工具调用"
    header += "]"
    body = [header, outcome.report]
    if outcome.truncated:
        body.append(
            f"[注意：{outcome.role} 的轮次已用尽，这份报告可能不完整。"
            "报告里未提及的部分不要当作已核实。]"
        )
    return "\n".join(body)


def enabled() -> bool:
    return settings.AGENT_DELEGATION_MODE in ("augment", "supervisor")


def describe_mode() -> str:
    """给启动日志用。"""
    mode = settings.AGENT_DELEGATION_MODE
    if mode not in ("augment", "supervisor"):
        return "off"
    return f"{mode} (roles: {', '.join(agent_roles.names())})"
