"""子代理执行器：跑完一次委派并把报告交回主代理。

它不复用调用方的那一圈状态机：编排层还要处理工单状态、审批挂起、轨迹落库、成本
累计与最终答复的透出，而子代理一样都不需要——它拿到的是**一句任务描述**，不是一张
工单；它的产出是给主代理读的报告，不流给客户看。把这些分支都塞进同一个执行器，
会让每个分支都得先判断"我现在是主代理还是子代理"。

三个关键约束：

1. **子代理看不到这张工单。** 只给它任务描述。这不是省事,是委派的全部意义所在:
   如果它还要读完整的工单与检索材料,那主代理直接自己做就行了,委派只多付了一次生成。
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
from services import agent_roles, guardrails, pricing, prompt_library
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
    # 这个子代理自己烧掉的模型成本。委派对主循环来说只是"一次工具调用",
    # 而它内部是一整个 3~5 轮的循环——不把这里的数字记回工单的成本账,
    # 文档§6.3 那条"单工单成本上限"就会在开委派之后被悄悄绕过。
    # 与 graph 里的取舍一致：None 是"不知道花了多少"，不是"没花钱"。
    cost: float | None = None
    cost_known: bool = False


def role_prompt(role: AgentRole) -> str:
    return prompt_library.get(role.prompt_key).render()


def repeat_limit() -> int:
    """子代理的重复调用上限。比主代理紧一档。

    ``AGENT_REPEAT_LIMIT`` 默认 3,是按主代理 6 轮的预算定的——放过两次相同调用
    占三分之一。子代理只有 3~5 轮,同一个比例意味着"先烧掉一半再拦",而拦下来
    那一刻剩下的轮次已经不够换一条路了。2026-08-28 那次 supervisor 失败的算术
    就是这个:4 轮预算,前两轮放过两次同样的无参调用,第三轮拦下,第四轮是最后
    一轮(不下发 schema),于是 ``web_search`` 一次都没机会被调到。

    取 ``min(2, 主上限)`` 而不是写死 2:主上限被调到 1(等于"一次都不许重复")时,
    子代理不该反而比它松。
    """
    main = settings.AGENT_REPEAT_LIMIT
    if main <= 0:
        # 0 表示关闭重复检测,子代理跟随
        return main
    return min(2, main)


class SubAgentRunner:
    """按角色跑一次委派。

    ``take_budget`` 是主循环的字符预算领取函数:子代理的工具结果和主代理的走同一份
    预算,签名只暴露一个函数而不是整个预算对象,是为了让
    "子代理只能花钱、不能查还剩多少、也不能改" 这件事由类型保证。

    ``sanitize_result`` 是主循环的协议中和函数（工单那侧是 ``graph._sanitize_plain``）。
    **这一道不能省**：主代理的工具结果是在 ``execute`` 节点里过中和的,而子代理的
    工具结果压根不经过那个节点——它在一个工具处理器的调用栈里直接进了子代理自己的
    messages。少这一道,一段写在订单备注里的 ``<function=call>`` 就能打乱文本工具协议
    模型对"哪句是工具结果"的判断,而文档§护栏要的正是"外部内容进上下文之前过一遍"。
    """

    def __init__(
        self,
        model_adapter: ModelAdapter,
        runtime: ToolRuntime,
        *,
        generation: dict[str, Any],
        take_budget: Callable[[str], str],
        sanitize_result: Callable[[str], str] | None = None,
    ) -> None:
        self._adapter = model_adapter
        self._runtime = runtime
        self._generation = generation
        self._take_budget = take_budget
        self._sanitize = sanitize_result or (lambda text: text)

    @property
    def _fallback_model(self) -> str:
        """成本换算用的模型名：提供商没在 span 上留模型时用它。

        从 ``generation`` 里取而不是加一个构造参数——那个字典本来就是"这次调用用什么
        模型、什么温度"，再传一次同名参数迟早会和这里的对不上。
        """
        return str(self._generation.get("model") or "")

    def _schemas_for(self, role: AgentRole) -> list[dict[str, Any]]:
        """该角色能用的工具 schema。

        取交集而不是照 ``role.tools`` 全给:没接政策检索的时候
        ``search_policy`` 根本没注册,给了它 schema 就是让它去调一个
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
        repeats = RepeatGuard(repeat_limit())
        max_rounds = max(1, role.max_rounds)
        report_parts: list[str] = []
        round_index = 0
        truncated = False
        # 逐轮累加，而不是在退出的那一刻读外层 span 的用量：外层那个
        # ``agent.<role>`` span 跨了 3~5 次模型调用，而提供商的 ``set_usage`` 是
        # **覆盖**写，跑完只剩最后一轮的 token 数——一次多轮委派会被记成"只花了
        # 一轮的钱"。所以每一轮单独开一个 LLM span，用完立刻换算、加总。
        cost_total = 0.0
        cost_seen = False

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

                async with tracer.span(
                    f"{role.name}.round",
                    SpanKind.LLM,
                    role=role.name,
                    round_index=round_index,
                    tool_count=len(schemas),
                ) as turn:
                    completion = await self._complete(messages, schemas, role)
                spent, known = pricing.cost_of_span(turn, self._fallback_model)
                if spent is not None:
                    cost_total += float(spent)
                cost_seen = cost_seen or known

                if completion is None:
                    span.set(failed=True)
                    return SubAgentOutcome(
                        role=role.name,
                        report="",
                        steps=steps,
                        rounds=round_index,
                        failed=True,
                        cost=cost_total or None,
                        cost_known=cost_seen,
                    )

                if completion.protocol_error:
                    span.set(protocol_error=True)
                    return SubAgentOutcome(
                        role=role.name,
                        report=completion.content or "",
                        steps=steps,
                        rounds=round_index,
                        failed=not completion.content.strip(),
                        cost=cost_total or None,
                        cost_known=cost_seen,
                    )

                calls = [] if is_final else completion.tool_calls
                if not calls:
                    if completion.content.strip():
                        report_parts.append(completion.content)
                    break

                messages.append(completion.as_assistant_message())
                text_results: list[str] = []
                # ``barren`` 只数**此路不通**的调用(工具不可用)。
                #
                # 重复调用**不进这个计数**,这是 2026-08-29 修 supervisor 那次失败
                # 的核心。``REPEATED`` 回灌的原话是"请改用不同的参数,或者基于已有
                # 信息直接作答"——原来的实现把它算进 barren,于是下一轮 schema 被
                # 清空,模型拿到了"换个工具"的建议却没有了执行它的手段。
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
                            # 相对更高:三轮里浪费一轮就是三分之一。所以子代理用
                            # 自己那个更紧的上限(见 repeat_limit)。
                            #
                            # 但**不算 barren**:被拦下不等于此路不通,恰恰相反,
                            # 那句纠正说明请它换参数或换工具,而换工具需要 schema。
                            result_text = repeated.content
                            status = repeated.status.value
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

                    content = self._take_budget(self._sanitize(result_text))
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
                    # 本轮每一次调用都**此路不通**(工具不可用)。工具坏了就是坏了,
                    # 把剩下的轮次全花在同一个失败上,等于把一次故障放大成一整个
                    # 委派。下一轮不给 schema,逼它用已有信息写报告。
                    max_rounds = round_index + 1
                # **重复调用不触发提前收敛。** 一条也不减。
                #
                # 先试过"连续两轮全是重复才收敛",仍然不对:那等于假设模型只需要
                # 一次提醒就会转向。评估里 web-vat-code 是连发三次才换工具的,
                # 两轮的规则照样在它转向前一轮把 schema 收走了。
                #
                # 为什么可以完全不收敛:被拦下的调用**根本没有执行**,它的成本是
                # 一次模型调用,没有任何工具副作用;而轮次总数由 max_rounds 兜着,
                # 所以浪费是有上界的。反过来,提前收走 schema 是在模型需要一次以上
                # 提醒时**保证失败**——有上界的浪费比保证失败好。
                #
                # 这正是 ToolStatus 把 REPEATED 与 UNAVAILABLE 分成两档的意义
                # (见 tool_runtime 里那段注释):前者"换个参数仍然值得试",而
                # "值得试"要求手里还有工具。

            report = "\n\n".join(part for part in report_parts if part.strip()).strip()
            span.set(
                rounds=round_index,
                steps=len(steps),
                report_chars=len(report) or None,
                truncated=truncated or None,
                repeated_blocked=repeats.blocked or None,
                cost=cost_total or None,
            )
            return SubAgentOutcome(
                role=role.name,
                report=report,
                steps=steps,
                rounds=round_index,
                truncated=truncated,
                failed=not report,
                cost=cost_total or None,
                cost_known=cost_seen,
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

    **这里只写契约,不写策略。** "什么时候值得委派"由调用方在系统上下文里给
    一段说明（工单那侧是 ``graph._delegation_notice``）。两件事分开的原因是:
    策略要说的那句"这一步自己做还是派人"取决于此刻注册了哪些工具,而 schema
    这段 description 是**按当前角色列表现场生成的**——把策略写死在这里,
    supervisor 模式下"能直接调工具解决的事自己做"就成了假话(那时主代理没有那些工具)。

    留在这里的是不随配置变的事实:子代理看不到工单原文(所以 task 必须自包含)、
    有哪些角色、各自能做什么。
    """
    lines = [
        "把一个独立的子任务交给专门的子代理执行，并拿回它的报告。",
        "子代理看不到这张工单，只能看到你在 task 里写的内容。",
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
                # 的参数都是单值(订单号、检索词),写错了模型能从报错里看出来,
                # 而一句写得含糊的 task 会拿回一份看起来很正常但查错方向的报告。
                # 所以这里给一正一反两个例子——提示词里反复讲"必须自包含"是抽象的,
                # 一个反例比三句叮嘱更能说明"含糊"长什么样。
                "description": (
                    "给子代理的任务描述。它看不到工单原文，所以背景、要查什么、"
                    "你已经知道的编号与事实、需要它核对的材料原文，都要写进这一段里。\n"
                    "好的例子：「核对订单 ORD20260115001 是否还能改地址。客户说想改到"
                    "上海。我已查到该单状态为 paid、收货地址在苏州。请给出订单当前状态、"
                    "是否已发货，以及如果已发货，物流到哪一步。」\n"
                    "不好的例子：「帮我查一下这个还能不能改」——子代理不知道"
                    "「这个」指哪张单、不知道要改什么，只能瞎查。"
                ),
            },
        },
        "required": ["role", "task"],
        "additionalProperties": False,
    }, "\n".join(lines)


def delegation_notice(roles: list[AgentRole], mode: str) -> str:
    """给主代理的那段"什么时候值得委派"，注入系统上下文。

    为什么是一段注入的消息而不是 ``prompts/ticket_agent/`` 里的一个版本分支：
    这段话里**必须列出此刻真正可用的角色**，而那份清单取决于本轮注册了哪些工具
    （没接政策检索时 policy 角色不存在，low 风险的工单没有资金类工具）。写进
    版本化的模板就只有两种可能：把清单写死（那它迟早和工具面对不上），或者加一个
    占位符（那 messages[0] 每换一张工单就变一次，整段提示词缓存作废——
    见 ``prompts/ticket_agent/v1.md`` 的 notes 与 ``graph._PLAN_NOTICE`` 同样的取舍）。

    策略本身（"一次能查完的自己做"）是不随工单变的那部分，它留在这里；
    随配置变的只有角色清单和 supervisor 那一句。
    """
    if not roles:
        return ""
    menu = "、".join(f"{role.name}（{role.summary.split('。')[0]}）" for role in roles)
    lines = [
        "[可以把独立的子任务交出去]",
        f"可用的子代理：{menu}。",
    ]
    if mode == "supervisor":
        lines.append(
            "这个模式下你自己没有查询工具——需要事实就派人查，拿到报告再继续。"
        )
    else:
        lines.append(
            "一次工具调用就能查完的事自己做：委派要多付一整个子代理循环的生成，"
            "而它看不到这张工单，你得把背景重述一遍，重述本身就是成本。"
        )
    lines += [
        "值得派人的情形：要跨好几张单据核对、要查政策原文，或者要给情绪激动的客户"
        "起草一段话——它的报告只带结论回来，比把一堆原文留在上下文里省。",
        "写操作一律由你自己提出（改地址、开票、退款、取消）。不要试图委派它们："
        "提出之后它们会在工单台上停下来等人批，而委派出去的执行不过那道闸门。",
        "task 参数必须自包含：订单号、已查到的状态与金额、要它核对的原文，都写进去。",
    ]
    return "\n".join(lines)


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
    if not outcome.steps:
        # 一次工具都没调就交了报告 = 凭记忆答的。
        #
        # 2026-08-28 的评估里 memory-web 与 recovery-search-down 都是这个形状
        # (``calls=[delegate]``、``stubQueries=[]``)。最危险的是后者:那道题考的
        # 恰恰是"查不到时如实说",而它编了一个具体汇率出来。
        #
        # 为什么标在这里而不是只靠提示词:主代理**看不到检索过程**,只看到这段
        # 正文,而一段凭记忆写的报告读起来和查过的一模一样。提示词里已经写了
        # "没有出处不要写成事实",但那句话给了模型一个合法出口(标成推测就行),
        # 而"有没有真的调过工具"是这边确定知道的事实,不该交给措辞去保证。
        body.append(
            f"[注意：{outcome.role} **没有调用任何工具**，这份报告未经检索，"
            "内容来自模型自身记忆。不要把其中任何具体数字、日期或条款当作已核实的事实；"
            "需要确凭的话请自己查，或告诉用户这部分未能核实。]"
        )
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
