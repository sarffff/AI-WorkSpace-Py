import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from sqlalchemy.orm import Session

from config import settings
from models import Chat, Message, MessageToolStep, User
from redis_service import redis_service
from services.clock import naive_now
from services.conversation_context import ConversationContextBuilder
from services.feedback_service import feedback_service
from services import guardrails
from services.guardrails import guard, mask_markup
from services.knowledge_service import KnowledgeService
from services.memory_service import memory_service
from services.model_adapter import (
    ModelAdapter,
    ModelCompletion,
    OpenAICompatibleAdapter,
    ToolCall,
)
from services import prompt_library
from services.semantic_cache import semantic_cache
from services.telemetry import (
    SpanKind,
    current_trace_id,
    set_span_defaults,
    tracer,
)
from services.token_budget import HistoryMessage
from services import agent_roles
from services import agent_state
from services import approval
from services import approval_audit
from services import checkpoint_store
from services import planner
from services import retrieval_index
from services import subagent
from services import tool_history
from services import workspace_service
from services.tool_runtime import (
    CircuitBreaker,
    RepeatGuard,
    ToolDefinition,
    ToolResult,
    ToolRuntime,
    ToolStatus,
)
from services import vision
from services import workspace_tools

logger = logging.getLogger("chat_service")


class _ToolResultBudget:
    """工具结果注入上下文的字符预算。

    Agent 每多一轮就会往 messages 里追加一份工具结果,不设上限时几轮之后必然
    超出上下文窗口。这里按"单次上限 + 总量上限"双重约束,总量耗尽后直接通知
    模型收敛。字符数只是 token 的粗略代理,换成真正的 tokenizer 是后续工作。
    """

    def __init__(self, total: int, per_call: int) -> None:
        self._remaining = max(0, total)
        self._per_call = max(0, per_call)

    @property
    def exhausted(self) -> bool:
        return self._remaining <= 0

    @property
    def remaining(self) -> int:
        return self._remaining

    def restore(self, remaining: int) -> None:
        """从快照恢复余额。

        不恢复就是"中断一次 = 预算重置一次":用户点一下同意,模型又拿到一整份
        12000 字符的额度。审批的语义是"这一次操作我批准了",不是"重新发一次预算"。
        """
        self._remaining = max(0, remaining)

    def take(self, text: str) -> str:
        limit = min(self._per_call, self._remaining)
        if limit <= 0:
            return "[上下文预算已用尽，工具结果未注入。请基于已获得的信息直接回答。]"
        if len(text) <= limit:
            self._remaining -= len(text)
            return text
        self._remaining = 0
        return text[:limit] + f"\n\n[结果过长已截断，原始长度 {len(text)} 字符]"


@dataclass(slots=True)
class _ToolScope:
    """一次回答里工具执行的作用域。

    知识库的可见单位是工作区(全员共享、admin 管理),而记忆、附件这些
    仍然是用户个人的——两个 id 必须一起传,否则总有一处作用域用错。

    ``history`` 在工具构建之前取回并回填(确认令牌按用户原话计算,搜索工具的
    指代消解也要靠它),工具处理器执行时直接读这个字段。
    默认空列表,于是没填的调用方(比如子代理)行为与改动前逐位相同。
    """

    user_id: str
    workspace_id: str
    is_admin: bool
    history: list[HistoryMessage] = field(default_factory=list)


class _Delegations:
    """一次回答里所有委派的共享状态。

    子代理是在一个工具处理器内部跑的,那里既拿不到 SSE 的生成器也不该替主循环
    决定轨迹怎么归属。所以结果先攒在 ``pending`` 里,由主循环在 ``delegate``
    这一步返回之后立刻取走——事件因此是成批发出的,而不是子代理边跑边发。
    这个取舍是有代价的:委派期间界面上只有一个"正在委派给 researcher"的状态,
    看不到它此刻在查第几次。换成实时推送需要把整条链路改成 async 队列,
    而收益只是一个进度条。
    """

    __slots__ = ("used", "limit", "pending")

    def __init__(self, limit: int) -> None:
        self.used = 0
        self.limit = max(0, limit)
        self.pending: list[subagent.SubAgentOutcome] = []

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def take(self) -> list[subagent.SubAgentOutcome]:
        outcomes = self.pending
        self.pending = []
        return outcomes


@dataclass(slots=True)
class _TurnContext:
    """一个回合里**不可序列化**的那一半。

    与 ``agent_state.TurnState`` 正好互补:那边是数据(能进数据库、能跨请求),
    这边是协作者(持有数据库会话、HTTP 客户端、埋点 span)。恢复时 state 从
    快照读出来,context 按 state 里的参数**重建**——所以重建必须是确定性的,
    这是"恢复"与"重新开始"唯一的区别所在。

    把两者分开是这次重构的全部收益:改动之前它们混在同一批局部变量里,
    于是没有任何一个子集是可以存下来的。
    """

    runtime: ToolRuntime
    budget: _ToolResultBudget
    repeats: RepeatGuard
    breaker: CircuitBreaker
    citations: list[dict]
    generation: dict[str, Any]
    scope: _ToolScope
    turn: Any
    delegations: _Delegations | None = None
    gated: frozenset[str] = frozenset()

    def sync_to(self, state: agent_state.TurnState) -> None:
        """把守卫余额写回 state。每次快照之前调。"""
        state.budget_remaining = self.budget.remaining
        counts, blocked = self.repeats.snapshot()
        state.repeat_counts = counts
        state.repeat_blocked = blocked
        consecutive, tripped = self.breaker.snapshot()
        state.breaker_consecutive = consecutive
        state.breaker_tripped = tripped
        if self.delegations is not None:
            state.delegations_used = self.delegations.used

    def restore_from(self, state: agent_state.TurnState) -> None:
        """把 state 里的余额灌回守卫。重建 context 之后立刻调。"""
        self.budget.restore(state.budget_remaining)
        self.repeats.restore(state.repeat_counts, state.repeat_blocked)
        self.breaker.restore(state.breaker_consecutive, state.breaker_tripped)
        if self.delegations is not None:
            self.delegations.used = state.delegations_used


class ChatService:
    """聊天持久化及提供商中立模型/工具编排服务。"""

    def __init__(self, model_adapter: ModelAdapter | None = None):
        self.model_adapter = model_adapter or OpenAICompatibleAdapter()
        # 缓存 KnowledgeService 以复用内部 HTTP 客户端,避免每次 RAG 调用重建
        self._knowledge_service: KnowledgeService | None = None
        self._context_builder: ConversationContextBuilder | None = None

    def _get_knowledge_service(self) -> KnowledgeService:
        """获取缓存的 KnowledgeService 单例 (按需创建)。"""
        if self._knowledge_service is None:
            self._knowledge_service = KnowledgeService()
        return self._knowledge_service

    def _get_context_builder(self) -> ConversationContextBuilder:
        if self._context_builder is None:
            self._context_builder = ConversationContextBuilder(self.model_adapter)
        return self._context_builder

    @staticmethod
    def _chats_cache_key(user_id: str) -> str:
        return f"ai_workspace:chats:{user_id}"

    @staticmethod
    def _invalidate_chats_cache(user_id: str) -> None:
        if redis_service.enabled and redis_service.client:
            redis_service.client.delete(ChatService._chats_cache_key(user_id))

    async def get_recent_chats(self, db: Session, user_id: str) -> list[dict]:
        cache_key = self._chats_cache_key(user_id)
        if redis_service.enabled and redis_service.client:
            raw = redis_service.client.get(cache_key)
            if raw:
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    pass

        chats = (
            db.query(Chat)
            .filter(Chat.user_id == user_id)
            .order_by(Chat.updated_at.desc())
            .limit(20)
            .all()
        )
        result = [
            {
                "id": chat.id,
                "title": chat.title,
                "createdAt": chat.created_at.isoformat(),
                "updatedAt": chat.updated_at.isoformat(),
            }
            for chat in chats
        ]
        if redis_service.enabled and redis_service.client:
            redis_service.client.set(cache_key, json.dumps(result, ensure_ascii=False), ex=60)
        return result

    async def get_chat_by_id(self, db: Session, chat_id: str) -> Chat | None:
        return db.query(Chat).filter(Chat.id == chat_id).first()

    async def rename_chat(self, db: Session, chat_id: str, title: str) -> Chat | None:
        chat = await self.get_chat_by_id(db, chat_id)
        if not chat:
            return None
        chat.title = title
        db.commit()
        db.refresh(chat)
        self._invalidate_chats_cache(chat.user_id)
        return chat

    async def delete_chat(self, db: Session, chat_id: str) -> bool:
        chat = await self.get_chat_by_id(db, chat_id)
        if not chat:
            return False
        user_id = chat.user_id
        # 这两张表都没有外键(理由见 models 里各自的说明),级联删不到,得显式清。
        # 反馈随对话删的取舍写在 feedback_service.discard_chat 的文档串里。
        tool_history.discard_chat(db, chat_id)
        feedback_service.discard_chat(db, chat_id)
        # 快照里存着整段 messages。用户删了对话却留下一份完整副本是说不过去的
        checkpoint_store.discard_chat(db, chat_id)
        db.delete(chat)
        db.commit()
        self._invalidate_chats_cache(user_id)
        return True

    async def get_chat_messages(self, db: Session, chat_id: str) -> list[dict]:
        messages = (
            db.query(Message)
            .filter(Message.chat_id == chat_id)
            .order_by(Message.created_at.asc(), Message.seq.asc())
            .all()
        )
        return [
            {
                "id": msg.id,
                "content": msg.content,
                "role": msg.role,
                "model": msg.model,
                "chatId": msg.chat_id,
                "createdAt": msg.created_at.isoformat(),
            }
            for msg in messages
        ]

    async def get_chat_tool_steps(self, db: Session, chat_id: str) -> list[dict]:
        """整段对话的工具轨迹，按执行顺序。

        与 ``get_chat_messages`` 分开返回,而不是塞进消息里:一条 assistant 消息
        对应的是"这一回合最后说了什么",工具步骤对应的是"这一回合做了什么",
        两者数量不对等(一个回合可能零步也可能六步),硬塞在一起前端得先拆一遍。
        """
        steps = (
            db.query(MessageToolStep)
            .filter(MessageToolStep.chat_id == chat_id)
            .order_by(
                MessageToolStep.created_at.asc(),
                MessageToolStep.round_index.asc(),
                MessageToolStep.call_index.asc(),
            )
            .all()
        )
        return [tool_history.serialize(step) for step in steps]

    async def create_chat(self, db: Session, user_id: str, title: str = "New Chat") -> Chat:
        chat = Chat(user_id=user_id, title=title)
        db.add(chat)
        db.commit()
        db.refresh(chat)
        self._invalidate_chats_cache(user_id)
        return chat

    async def save_message(
        self,
        db: Session,
        chat_id: str,
        role: str,
        content: str,
        model: str | None = None,
        message_id: str | None = None,
    ) -> Message:
        if message_id:
            existing = db.query(Message).filter(Message.id == message_id).first()
            if existing:
                if (
                    existing.chat_id != chat_id
                    or existing.role != role
                    or existing.content != content
                ):
                    raise ValueError("消息 ID 已被其他消息使用")
                return existing

        message = Message(
            id=message_id,
            chat_id=chat_id,
            role=role,
            content=content,
            model=model,
            created_at=naive_now(),
        )
        db.add(message)
        chat = db.query(Chat).filter(Chat.id == chat_id).first()
        if chat:
            chat.updated_at = naive_now()
        db.commit()
        db.refresh(message)
        if chat:
            self._invalidate_chats_cache(chat.user_id)
        return message

    async def revise_user_message(
        self,
        db: Session,
        chat_id: str,
        message_id: str,
        content: str | None = None,
    ) -> Message | None:
        """保留指定用户消息,删除其后分支的所有冗余消息。"""
        messages = (
            db.query(Message)
            .filter(Message.chat_id == chat_id)
            .order_by(Message.created_at.asc(), Message.seq.asc())
            .all()
        )
        target_index = next(
            (index for index, message in enumerate(messages) if message.id == message_id),
            None,
        )
        if target_index is None:
            return None
        target = messages[target_index]
        if target.role != "user":
            return None

        for obsolete in messages[target_index + 1 :]:
            db.delete(obsolete)
        # 连带清掉这些回合的工具轨迹,并且**包括 target 自己那一回合**:
        # 它马上就要重新生成,留着旧轨迹会让模型以为该检索已经做过而直接跳过。
        tool_history.discard(
            db, chat_id, [message.id for message in messages[target_index:]]
        )
        if content is not None:
            target.content = content

        chat = db.query(Chat).filter(Chat.id == chat_id).first()
        if chat:
            chat.updated_at = naive_now()
        db.commit()
        db.refresh(target)
        if chat:
            self._invalidate_chats_cache(chat.user_id)
        return target

    async def _get_chat_history_messages(
        self,
        db: Session,
        chat_id: str,
        limit: int | None = None,
        exclude_message_id: str | None = None,
    ) -> list[HistoryMessage]:
        """取回历史备选。留多少条由 token 预算决定,这里只限制数据库读取量。"""
        query = db.query(Message).filter(Message.chat_id == chat_id)
        if exclude_message_id:
            query = query.filter(Message.id != exclude_message_id)
        messages = (
            query.order_by(Message.created_at.desc(), Message.seq.desc())
            .limit(limit or settings.HISTORY_FETCH_LIMIT)
            .all()
        )
        messages.reverse()
        return [
            HistoryMessage(id=message.id, role=message.role, content=message.content)
            for message in messages
            if message.role in {"user", "assistant", "system"}
        ]

    @staticmethod
    def _system_template(
        use_rag: bool, version: str | None = None
    ) -> prompt_library.PromptTemplate:
        """挑出这一轮要用的系统提示词版本。正文在 prompts/<key>/<version>.md。

        先拿模板、再渲染,是因为"用了哪一版"要在渲染之前就知道:
        它既要落进埋点(否则回头看 trace 只知道答得不好,不知道是哪版答的),
        也要进语义缓存的键(否则切版本后第一个问题命中的还是旧版答案)。
        """
        if not use_rag:
            return prompt_library.get("chat_system_plain")
        return prompt_library.get("chat_system_rag", version)

    @staticmethod
    def _system_prompt(
        template: prompt_library.PromptTemplate, prefetched: bool
    ) -> str:
        """渲染系统提示词。条件开关按模板声明传。

        没声明 ``prefetched`` 的版本(比如关掉 RAG 那一版)硬传进去会直接抛错,
        这正是想要的:换版本时漏改调用方,应该在这里炸,而不是静默少一段约束。

        ``PROMPT_CACHE_STABLE_PREFIX`` 开启时一律按 ``prefetched=False`` 渲染。
        提供商的上下文缓存是隐式的、按前缀逐字匹配的,而这条消息是整个前缀的第一
        条——它随预检索命中与否在两种正文之间来回切,等于每次翻转都把整段缓存作废。
        那句"已预检索过、不要重复检索"改由用户消息携带(见 ``_run_turn``),
        它讲的本来就是"本轮发生了什么",和预检索没命中时那句提示同属一类。
        """
        if settings.PROMPT_CACHE_STABLE_PREFIX:
            prefetched = False
        flags = {"prefetched": prefetched} if "prefetched" in template.flags else None
        return template.render(flags=flags)

    @staticmethod
    def _user_content(
        text: str, turn: Any, model: str | None
    ) -> str | list[dict[str, Any]]:
        """当前这一条用户消息:有图且模型支持视觉时换成内容块,否则原样返回。

        只处理当前这一条。历史消息在 token_budget 那边是纯字符串,把内容块塞进
        历史会让预算裁剪和滚动摘要一起失效;而且重发历史图片每轮都要再付一次
        图像 token,代价与收益完全不成比例。
        """
        result = vision.build_user_content(text, model=model or settings.LLM_MODEL)
        if result.images:
            turn.set(vision_images=result.images)
        if result.skipped:
            # 用户抱怨"它没看见我的图"时,这里能直接回答是模型不支持、图太大,
            # 还是路径解析不到。只记原因标签,不记路径与文件名。
            turn.set(vision_skipped=result.skipped[:5])
        return result.content

    @staticmethod
    def _guardrail_event(
        report: guardrails.ScanReport, *, round_index: int
    ) -> dict[str, Any]:
        """把护栏命中告知前端。

        对外只给规则名和分数,不回传命中的原文——那段文本本来就是可疑输入,
        原样转发到界面等于把注入内容又渲染了一遍。
        """
        return {
            "type": "guardrail",
            "findings": list(report.findings),
            "score": report.score,
            "masked": report.replacements,
            "blocked": report.blocked,
            "round": round_index,
        }

    def _create_tools(
        self,
        db: Session,
        scope: "_ToolScope",
        use_rag: bool,
        citation_sink: list[dict] | None = None,
        approvals: workspace_tools._ToolApprovals | None = None,
    ) -> list[ToolDefinition]:
        """本轮下发给模型的工具面。

        分两组、各自受控:知识库那三个由 ``use_rag`` 决定(界面上的「知识库」开关),
        workspace 那几个由自己的开关决定。二者解耦是必要的——查天气或算数不需要
        知识库,把它们绑在同一个开关上,用户关掉知识库就连计算器都没了。

        workspace 工具默认全部关闭,所以默认行为与只有知识库工具时逐位相同。
        ``approvals`` 是破坏性操作的确认令牌(按用户原话计算),传给 workspace
        工具的构建器;缺省即全拒。
        """
        tools: list[ToolDefinition] = []
        if use_rag:
            tools.extend(self._create_knowledge_tools(db, scope, citation_sink))
        tools.extend(
            workspace_tools.build(db, scope, self._get_knowledge_service(), approvals)
        )
        return tools

    def _approvals_for(
        self, prompt: str, history: list[HistoryMessage]
    ) -> workspace_tools._ToolApprovals:
        """本回合工具执行需要的用户授权。

        确认令牌的全部意义在于"用户真的要求过"这件事，只能由拿得到用户原话的
        一方来判定——主代理看得到，子代理看不到（所以写操作永远不给子代理）。
        这里扫的是当前问题与近期历史里**用户的消息原文**，而不是模型对原话的
        转述：转述可能掺入资料或网页里夹带的指令，原文不会。
        """
        texts = [prompt]
        for message in reversed(history):
            if message.role == "user":
                texts.append(message.content or "")
            if len(texts) >= 3:
                break
        joined = "\n".join(texts)
        return workspace_tools._ToolApprovals(
            delete_granted=workspace_tools.detect_delete_intent(joined)
        )

    @staticmethod
    def _supervisor_tools(
        tools: list[ToolDefinition], roles: list[agent_roles.AgentRole]
    ) -> list[ToolDefinition]:
        """supervisor 模式下主代理自己还留着哪些工具。

        把已被某个角色接管的工具收走,剩下的留给主代理——目前就是
        ``save_to_knowledge_base``:它需要用户明确要求才能执行,而子代理看不到
        用户原话,判断不了"是否真的要求保存"(理由写在 agent_roles 的模块文档里)。

        收走专用工具的代价很实在:简单问题也必须先委派一次才能查一下知识库,
        多付一次完整的子代理循环。这正是 ``augment`` 模式存在的原因,也是
        默认不开 ``supervisor`` 的原因。
        """
        owned = {name for role in roles for name in role.tools}
        return [tool for tool in tools if tool.name not in owned]

    @staticmethod
    def _create_delegate_tool(
        roles: list[agent_roles.AgentRole],
        runner: subagent.SubAgentRunner,
        state: _Delegations,
    ) -> ToolDefinition:
        """``delegate``:主代理把子任务交出去的那个工具。

        它是一个普通工具,而不是循环里的一个特殊分支——于是"要不要委派、派给谁"
        完全由模型在运行时决定,和它决定要不要检索是同一件事。做成分支就等于
        把这个决定挪回编码时,那样它是流水线,不是 agent。
        """
        schema, description = subagent.build_delegate_schema(roles)
        valid = {role.name: role for role in roles}

        async def delegate(arguments: dict[str, Any]) -> str:
            role_name = arguments.get("role")
            task = arguments.get("task")
            if not isinstance(role_name, str) or role_name not in valid:
                return (
                    f"委派失败：没有 {role_name!r} 这个子代理。"
                    f"可用：{', '.join(valid)}。"
                )
            if not isinstance(task, str) or not task.strip():
                return "委派失败：task 必须是非空字符串，且要把背景写全。"
            # 上限在这里挡,而不是靠提示词劝:超了就告诉它自己动手,
            # 而不是静默拒绝——静默拒绝的话它下一轮还会再派一次。
            if state.exhausted:
                return (
                    f"委派失败：本次回答的委派次数已达上限（{state.limit} 次）。"
                    "请自己调用工具完成剩下的部分，或基于已有信息作答。"
                )

            state.used += 1
            outcome = await runner.run(valid[role_name], task.strip())
            state.pending.append(outcome)
            return subagent.format_report(outcome)

        return ToolDefinition(
            name="delegate",
            description=description,
            parameters=schema,
            handler=delegate,
        )

    def _build_tool_surface(
        self,
        base_tools: list[ToolDefinition],
        *,
        generation: dict[str, Any],
        take_budget: Any,
        breaker: CircuitBreaker,
        turn: Any,
    ) -> tuple[ToolRuntime, _Delegations | None]:
        """组装本回合真正下发的工具面（含委派）。

        新回合和**中断恢复**都走这一个函数。这不是为了少写二十行:恢复要求
        "同样的参数得到同一套工具面",两条各自构建的路径迟早会漂移,而漂移的
        表现是"恢复之后行为微妙地不一样"——比在这里多一层间接要难查得多。

        子代理的运行时拿的是**未经角色过滤的**完整工具集合,按角色过滤发生在
        ``SubAgentRunner._schemas_for``(下发哪些 schema)和它的执行前检查(越权拦截)
        两处。让运行时本身只装该角色的工具也能做到,但那样每次委派都要重建一次
        ToolRuntime,而这几个工具里有的持有数据库会话与 HTTP 客户端。

        熔断器各自持有:子代理里的失败不该把主代理的工具熔断掉,反之亦然——
        它们面对的是不同的模型行为,不该互相牵连。
        """
        delegations: _Delegations | None = None
        tools = base_tools
        if subagent.enabled():
            registered = {tool.name for tool in base_tools}
            roles = agent_roles.available(registered)
            if roles:
                delegations = _Delegations(settings.AGENT_MAX_DELEGATIONS)
                runner = subagent.SubAgentRunner(
                    self.model_adapter,
                    ToolRuntime(
                        base_tools,
                        CircuitBreaker(settings.TOOL_CIRCUIT_BREAKER_FAILURES),
                    ),
                    generation=generation,
                    take_budget=take_budget,
                )
                if settings.AGENT_DELEGATION_MODE == "supervisor":
                    tools = self._supervisor_tools(base_tools, roles)
                tools = [
                    *tools,
                    self._create_delegate_tool(roles, runner, delegations),
                ]
                turn.set(
                    delegation_mode=settings.AGENT_DELEGATION_MODE,
                    delegation_roles=[role.name for role in roles],
                )
        return ToolRuntime(tools, breaker), delegations

    def _create_knowledge_tools(
        self,
        db: Session,
        scope: "_ToolScope",
        citation_sink: list[dict] | None = None,
    ) -> list[ToolDefinition]:
        knowledge_service = self._get_knowledge_service()

        async def search_knowledge(arguments: dict[str, Any]) -> str:
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return "检索失败：query 必须是非空字符串。"
            # 循环里的检索同样要过指代消解。改动前它只作用于预检索,于是
            # "那它的赔偿标准呢"这类省略式追问在**第一轮**被改写、在模型自己
            # 发起的后续检索里却没有——而模型很容易把用户的原话直接抄进 query。
            # 表现是同一个问题预检索命中、工具检索漂掉,极难归因。
            search_query = await self._condense_query(scope.history, query.strip())
            context, citations = await knowledge_service.build_rag_context_with_citations(
                db,
                search_query,
                scope.workspace_id,
                top_k=settings.RAG_TOP_K,
                # 检索范围 = 工作区共享文档 + 这个用户自己的私有文档。
                # 漏传 viewer_id 的后果是模型永远看不到用户的个人资料,
                # 而那正是 chat 附件默认存进去的地方。
                viewer_id=scope.user_id,
            )
            # 工具处理器不能直接产出 SSE 事件,命中的引用先放进 sink,由循环负责发出
            if citation_sink is not None:
                citation_sink.extend(citations)
            return context or (
                "本地知识库中未找到达到相关度要求的参考内容。"
                "可以换一种说法重新检索，或调用 list_knowledge_documents 查看有哪些文档。"
            )

        async def list_knowledge_documents(_arguments: dict[str, Any]) -> str:
            documents = await knowledge_service.get_documents(db, scope.workspace_id)
            indexed = [doc for doc in documents if doc["status"] == "indexed"]
            if not indexed:
                return "本地知识库中没有已索引的文档。"
            lines = [f"知识库中共有 {len(indexed)} 个已索引文档："]
            # 文件名也是外部输入,一个精心命名的文档能在列表里伪造出一条参考资料
            lines += [
                f"- {mask_markup(doc['name'])}（document_id: {doc['id']}，"
                f"分块数: {doc['chunks']}）"
                for doc in indexed
            ]
            return "\n".join(lines)

        async def read_document_chunk(arguments: dict[str, Any]) -> str:
            document_id = arguments.get("document_id")
            chunk_index = arguments.get("chunk_index")
            if not isinstance(document_id, str) or not document_id.strip():
                return "读取失败：document_id 必须是非空字符串。"
            if not isinstance(chunk_index, int) or chunk_index < 0:
                return "读取失败：chunk_index 必须是非负整数。"

            chunks = await knowledge_service.read_chunks(
                db, scope.workspace_id, document_id.strip(), chunk_index
            )
            if not chunks:
                return (
                    "读取失败：未找到该文档或分块。"
                    "请调用 list_knowledge_documents 确认可用的 document_id。"
                )
            name = mask_markup(chunks[0]["document_name"])
            # 定向读取绕过了检索排序,是注入面最直接的一条路径,同样要过护栏
            body = "\n\n".join(
                f"【{name} · 分块 {chunk['chunk_index']}】\n{guard.sanitize(chunk['content'])[0]}"
                for chunk in chunks
            )
            shielded, _report = guard.shield(
                body, label="文档内容", kind="read_document_chunk"
            )
            return shielded

        return [
            ToolDefinition(
                name="search_knowledge_base",
                description=(
                    "按语义检索本地知识库中与问题相关的文档分块。"
                    "返回内容包含 document_id 与分块号，可用于后续定向读取。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "检索语句，应为问题的核心关键词或改写后的搜索语句",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=search_knowledge,
            ),
            ToolDefinition(
                name="list_knowledge_documents",
                description=(
                    "列出本地知识库中已索引的全部文档（名称、document_id、分块数）。"
                    "适合在不确定知识库里有什么资料时先调用。"
                ),
                parameters={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                handler=list_knowledge_documents,
            ),
            ToolDefinition(
                name="read_document_chunk",
                description=(
                    "读取指定文档的某个分块及其相邻分块，用于补全检索结果里"
                    "被切断的上下文。document_id 与 chunk_index 来自检索结果。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "description": "目标文档的 document_id",
                        },
                        "chunk_index": {
                            "type": "integer",
                            "description": "目标分块的序号，从 0 开始",
                        },
                    },
                    "required": ["document_id", "chunk_index"],
                    "additionalProperties": False,
                },
                handler=read_document_chunk,
            ),
        ]

    async def _condense_query(
        self, history: list[HistoryMessage], prompt: str
    ) -> str:
        """把追问改写成自包含问题再做预检索。

        "那它的赔偿标准呢?"拿原文去检索会因缺少指代对象而召回漂移。改写是
        增强,不是依赖:任何失败(模型故障、空输出、输出长得像抄了一遍对话)
        都回退原文。首轮没有历史,直接原样返回,不花这次调用。
        """
        if not settings.RAG_CONDENSE_QUERY or not history:
            return prompt
        recent = history[-6:]
        turns = "\n".join(
            f"{message.role}: {message.content[:400]}" for message in recent
        )
        try:
            content = prompt_library.render(
                "rag_query_condense", recent_turns=turns, question=prompt
            )
            completion = await self.model_adapter.complete(
                messages=[{"role": "user", "content": content}],
                tools=[],
                model=settings.utility_model,
                temperature=0.0,
                max_tokens=settings.RAG_CONDENSE_MAX_TOKENS,
                purpose="query_condense",
            )
        except Exception as exc:
            logger.warning("query condense failed: %s", type(exc).__name__)
            return prompt
        # 长度检查针对整个输出而不是第一行:把整段对话抄一遍的"改写"第一行
        # 可能很短,单看第一行拦不住
        raw = (completion.content or "").strip()
        if not raw:
            # 空输出以前和"改写没必要"共用同一条静默 return。它们不是一回事:
            # 这一条是故障。原来 max_tokens=256 时它 100% 走这里——推理模型把
            # 预算花在思考上,一个字都没吐——于是指代消解从未生效,而系统提示词
            # 仍然告诉模型"已经预检索过了",等于把弱检索包装成"已查过"。
            logger.warning(
                "query condense produced no output (finish_reason=%s, max_tokens=%s); "
                "falling back to the raw follow-up question",
                completion.finish_reason,
                settings.RAG_CONDENSE_MAX_TOKENS,
            )
            return prompt
        if len(raw) > len(prompt) * 3 + 200:
            return prompt
        condensed = raw.splitlines()[0].strip().strip('"“”')
        if not condensed:
            return prompt
        return condensed

    async def _prefetch_rag_context(
        self, db: Session, workspace_id: str, prompt: str, viewer_id: str | None = None
    ) -> tuple[str, list[dict], bool]:
        """首轮之前的一次性检索。失败时返回空内容并标记 failed,不影响主流程。

        ``viewer_id`` 决定能不能检索到这个用户的私有文档。默认 None(只查共享)
        是刻意的收紧方向:漏传只会少检索到东西,反过来是越权。
        """
        try:
            context, citations = (
                await self._get_knowledge_service().build_rag_context_with_citations(
                    db,
                    prompt,
                    workspace_id,
                    top_k=settings.RAG_TOP_K,
                    viewer_id=viewer_id,
                )
            )
            return context, citations, False
        except Exception as exc:
            logger.warning("RAG prefetch failed: %s", type(exc).__name__)
            return "", [], True

    async def _complete_fallback(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        generation: dict[str, Any],
    ) -> ModelCompletion | None:
        """流式不可用时的非流式兜底。彻底失败返回 None,由调用方上报错误。"""
        try:
            return await self.model_adapter.complete(
                messages=messages, tools=tools, **generation
            )
        except Exception as exc:
            logger.error("complete fallback failed: %s", type(exc).__name__)
            return None

    async def stream_ai_response(
        self,
        db: Session,
        user_id: str,
        chat_id: str,
        prompt: str,
        model: str | None = None,
        use_rag: bool = False,
        message_id: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
        prompt_version: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """在一条 trace 下驱动 Agent 循环。

        埋点包在最外层：``chat.turn`` 是根 span，一次回答里的每次模型调用、
        工具执行、检索、向量化都挂在它下面。于是「这次回答花了多少钱、
        时间耗在哪一段、走了几轮」变成一次 SQL 查询就能回答的问题。

        ``prompt_version`` 只作用于这一次请求,不改全局设置——A/B 提示词时
        两个人可以同时用不同版本互不干扰,这是"改全局开关"做不到的。
        """
        system_template = self._system_template(use_rag, prompt_version)
        # 工具与检索的作用域:知识库按工作区共享(旧用户/OAuth 用户在此懒初始化)
        user = db.query(User).filter(User.id == user_id).first()
        workspace = workspace_service.resolve_for_user(db, user) if user else None
        scope = _ToolScope(
            user_id=user_id,
            workspace_id=workspace.id if workspace else user_id,
            is_admin=bool(
                user and user.role == workspace_service.ROLE_ADMIN
            ),
        )
        async with tracer.trace(
            user_id=user_id, chat_id=chat_id, message_id=message_id
        ) as trace:
            async with tracer.span(
                "chat.turn",
                SpanKind.AGENT,
                model=model or settings.LLM_MODEL,
                use_rag=use_rag,
                prefetch=settings.RAG_PREFETCH if use_rag else None,
                # 提示词缓存的 A/B 维度:事后要能按它分组比 cache_hit_ratio,
                # 否则"稳定前缀到底提了多少命中率"只能靠感觉。
                stable_prefix=settings.PROMPT_CACHE_STABLE_PREFIX,
                # 只记版本号,不记正文:span 属性里永不出现提示词内容
                prompt_version=system_template.ref,
            ) as turn:
                resolved_model = model or settings.LLM_MODEL
                # 语义缓存的分桶键。开 RAG 时**必须带上用户**:回答可能引用了这个人的
                # 私有文档,而按工作区分桶会把它命中给同空间的另一个人——这个模块
                # 自己的文档第一条就写着"跨用户命中就是数据泄露,不是优化"。
                # 工作区分桶是共享知识库那一轮加的,私有文档让它的前提不再成立。
                #
                # 关 RAG 时仍按工作区分桶:回答不依赖任何文档,跨用户命中是安全的。
                # 代价是开 RAG 时失去跨用户命中——包括那些其实只引用了共享文档的
                # 回答。查询时还不知道会检索到什么,所以这里只能按最坏情况取。
                cache_scope = (
                    retrieval_index.scope_key(scope.workspace_id, scope.user_id)
                    if use_rag
                    else scope.workspace_id
                )
                hit = await semantic_cache.lookup(
                    cache_scope,
                    prompt,
                    resolved_model,
                    use_rag,
                    prompt_ref=system_template.ref,
                )
                if hit is not None:
                    turn.set(
                        cache_hit=True,
                        cache_exact=hit.exact,
                        cache_similarity=round(hit.similarity, 4),
                    )
                    yield {
                        "type": "cache_hit",
                        "similarity": round(hit.similarity, 4),
                        "exact": hit.exact,
                        "tokensSaved": hit.entry.tokens_saved,
                    }
                    yield {"type": "message_delta", "content": hit.entry.answer}
                    return

                # 只把"干净地跑完一整轮"的回答写进缓存:出过错、被中断、
                # 或者护栏拦过的回答复用一次就是错两次。
                answer_parts: list[str] = []
                cacheable = True
                async for event in self._run_turn(
                    db,
                    user_id,
                    scope,
                    chat_id,
                    prompt,
                    model,
                    use_rag,
                    message_id,
                    temperature,
                    max_tokens,
                    top_p,
                    turn,
                    system_template,
                ):
                    if event["type"] == "message_delta":
                        answer_parts.append(event["content"])
                    elif event["type"] in ("error", "guardrail"):
                        cacheable = False
                    yield event

                answer = "".join(answer_parts)
                if cacheable and answer.strip():
                    # 省下的 token 用这一轮实际消耗来标记；埋点关掉时只能记 0，
                    # 面板上就会看到"命中了但省了 0 token"——那是缺埋点，不是没省。
                    prompt_tokens = sum(
                        span.prompt_tokens or 0 for span in getattr(trace, "spans", [])
                    )
                    completion_tokens = sum(
                        span.completion_tokens or 0
                        for span in getattr(trace, "spans", [])
                    )
                    await semantic_cache.store(
                        # 和 lookup 用同一个桶键,否则存进去的永远命中不到
                        cache_scope,
                        prompt,
                        answer,
                        resolved_model,
                        use_rag,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        prompt_ref=system_template.ref,
                    )

    @staticmethod
    async def _emit_subagent_steps(
        db: Session,
        outcome: subagent.SubAgentOutcome,
        *,
        state: agent_state.TurnState,
        round_index: int,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """把子代理内部轨迹翻译成主 SSE 流，并逐步落库。

        子代理有自己的轮次，但把那个数字直接塞进 ``round`` 会和主代理轮次冲突：
        UI 会把 researcher 的第 1 轮排到主代理第 1 轮旁边，看起来像并行调用。
        所以 SSE 的 ``round`` 保持外层主代理轮次，内层轮次单独放 ``agentRound``。
        数据库仍用外层轮次排序，``agent_role`` 用来区分归属。

        每次委派额外开一行 ``agent_runs``（``parent_run_id`` 指向主代理）。子代理跑完
        才建这行、并且直接落终态：它是在一个工具处理器内部同步跑完的，没有"正在
        运行"这个可观察的中间态可言。这样"这次回答起了几个子代理、哪个失败了、
        哪个是半截报告"变成一次 SQL 查询，而不是按 ``(agent_role, 时间)`` 去推断
        ——一回合里委派两个同角色子代理时，推断必然出错。
        """
        child_run_id = str(uuid.uuid4())
        if checkpoint_store.enabled():
            checkpoint_store.start_run(
                db,
                run_id=child_run_id,
                chat_id=state.chat_id,
                user_id=state.user_id,
                message_id=state.message_id,
                model=None,
                prompt_ref=None,
                parent_run_id=state.run_id,
                agent_role=outcome.role,
            )
            checkpoint_store.update_run(
                db,
                child_run_id,
                status="failed" if outcome.failed else "done",
                rounds=outcome.rounds,
                error_type="subagent_failed" if outcome.failed else None,
                finished=True,
            )

        for index, step in enumerate(outcome.steps):
            yield {
                "type": "agent_step",
                "agent": outcome.role,
                "phase": "tool_start",
                "tool": step.tool,
                "input": step.arguments,
                "round": round_index,
                "agentRound": step.round_index,
            }
            yield {
                "type": "agent_step",
                "agent": outcome.role,
                "phase": "tool_result",
                "tool": step.tool,
                "status": step.status,
                "round": round_index,
                "agentRound": step.round_index,
            }
            # call_index 乘一个固定槽宽再加内部序号:外层同一轮里可能委派多次,
            # 而每个子代理的 call_index 都从 0 开始。不做偏移,落库后的稳定排序
            # 会把几次委派里的第 0 步全排在一起。
            tool_history.record(
                db,
                chat_id=state.chat_id,
                message_id=state.message_id,
                round_index=round_index,
                call_index=10_000 + index,
                tool_name=step.tool,
                status=step.status,
                result=step.result,
                tool_call_id=step.tool_call_id,
                arguments=step.arguments,
                citations=step.citations,
                agent_role=outcome.role,
                run_id=child_run_id,
            )
        yield {
            "type": "agent_state",
            "agent": outcome.role,
            "status": "failed" if outcome.failed else "completed",
            "round": round_index,
            "rounds": outcome.rounds,
            "steps": len(outcome.steps),
            "truncated": outcome.truncated,
            "runId": child_run_id,
        }

    async def _run_turn(
        self,
        db: Session,
        user_id: str,
        scope: _ToolScope,
        chat_id: str,
        prompt: str,
        model: str | None,
        use_rag: bool,
        message_id: str | None,
        temperature: float,
        max_tokens: int,
        top_p: float,
        turn: Any,
        system_template: prompt_library.PromptTemplate,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """驱动 Agent 循环,产出与业务层无关的 SSE 事件流。

        每一轮:流式调用模型 -> 若模型请求工具则执行并把结果回灌 messages -> 下一轮。
        循环在下列任一条件下收敛到最终回答:模型不再请求工具、轮次用尽、
        工具结果预算耗尽、或本轮请求的工具全部没带回新内容(不可用或重复调用)。
        """
        citations: list[dict] = []
        budget = _ToolResultBudget(
            settings.TOOL_RESULT_TOTAL_CHARS, settings.TOOL_RESULT_MAX_CHARS
        )
        # 重复调用检测。作用域是一次回答——跨回合不算重复:用户第二次问同一件事时
        # 重新检索一遍是对的,那时知识库可能已经变了。
        repeats = RepeatGuard(settings.AGENT_REPEAT_LIMIT)
        # 熔断器显式持有(改动前是内联在 ToolRuntime 构造里):它的状态要进快照,
        # 否则中断一次就能让一个正在连续失败的工具复活。
        breaker = CircuitBreaker(settings.TOOL_CIRCUIT_BREAKER_FAILURES)
        generation: dict[str, Any] = {
            "model": model or settings.LLM_MODEL,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }
        run_id = str(uuid.uuid4())

        # 历史要先于工具构建取回：确认令牌要按用户原话计算，而工具处理器
        # 执行时还要用它做指代消解（scope.history 在这里一起回填）。
        history = await self._get_chat_history_messages(
            db, chat_id, exclude_message_id=message_id
        )
        scope.history = history
        approvals = self._approvals_for(prompt, history)
        base_tools = self._create_tools(db, scope, use_rag, citations, approvals)
        # ---- 多代理:委派 ----
        # 子代理的运行时拿的是**未经角色过滤的**完整工具集合,按角色过滤发生在
        # SubAgentRunner._schemas_for(下发哪些 schema)和它的执行前检查(越权拦截)
        # 两处。让运行时本身只装该角色的工具也能做到,但那样每次委派都要重建一次
        # ToolRuntime,而这几个工具里有的持有数据库会话与 HTTP 客户端。
        # 熔断器单独实例、各自持有:子代理里的失败不该把主代理的工具熔断掉,
        # 反之亦然——它们面对的是不同的模型行为,不该互相牵连。
        runtime, delegations = self._build_tool_surface(
            base_tools,
            generation=generation,
            take_budget=budget.take,
            breaker=breaker,
            turn=turn,
        )
        context = await self._get_context_builder().build(chat_id, history)
        if context.compacted:
            yield {
                "type": "context_compacted",
                "summarized": context.summarized,
                "kept": context.kept,
            }

        # ---- 上一回合的工具轨迹 ----
        # 回合内工具结果靠 messages 回灌,回合一结束那个列表就没了。这里把落库的
        # 轨迹按预算取回来,否则模型对自己上一回合读过什么完全没有记忆。
        trajectory, trajectory_steps = tool_history.build_messages(
            db,
            chat_id,
            exclude_message_id=message_id,
            tools_available=bool(runtime.schemas),
        )
        if trajectory_steps:
            turn.set(tool_history_steps=trajectory_steps)

        # ---- RAG 预检索 ----
        # 开启时先检索一次并注入用户消息,保证即使模型不调工具也能看到知识库内容;
        # 代价是每轮固定消耗一次检索,且与模型自主检索可能重复,
        # 因此系统提示词会明确告知模型"已预检索过,不要重复检索"。
        # 预检索以 tool_start/tool_result 事件对外呈现,前端才能显示
        # 「检索知识库...」->「检索知识库完成,正在思考下一步...」状态。
        user_content = prompt
        prefetched = False
        # 显式初始化而不是只在预检索块里赋值:下面规划那段要读它。靠 prefetched
        # 短路虽然当前也不会 NameError,但那是个只对"求值顺序"成立的巧合,
        # 而这两处离得很远。
        prefetch_context = ""
        # 条件是 use_rag 而不是"有没有工具":workspace 工具打开之后,关掉知识库的
        # 请求也会有非空的 tools,拿它当代理会让预检索在 RAG 关闭时照样触发。
        if use_rag and settings.RAG_PREFETCH:
            search_query = await self._condense_query(history, prompt)
            yield {
                "type": "tool_start",
                "tool": "search_knowledge_base",
                "input": {"query": search_query},
                "round": 0,
            }
            # 护栏埋在检索链路深处,用一个作用域收集器把命中情况带回这里
            with guardrails.collecting() as reports:
                prefetch_context, prefetch_citations, prefetch_failed = (
                    await self._prefetch_rag_context(
                        db, scope.workspace_id, search_query, viewer_id=scope.user_id
                    )
                )
            prefetch_report = guardrails.summarize(reports)
            yield {
                "type": "tool_result",
                "tool": "search_knowledge_base",
                "status": "error" if prefetch_failed else "ok",
                "round": 0,
            }
            if prefetch_report is not None:
                yield self._guardrail_event(prefetch_report, round_index=0)
            # 预检索也是轨迹的一步(round=0)。不记的话下一回合看不出这次回答的
            # 参考内容是怎么来的,而且它常常是整段对话里唯一真正做过的检索。
            tool_history.record(
                db,
                chat_id=chat_id,
                message_id=message_id,
                round_index=0,
                call_index=0,
                tool_name="search_knowledge_base",
                status="error" if prefetch_failed else "ok",
                result=prefetch_context,
                arguments={"query": search_query},
                citations=prefetch_citations,
                run_id=run_id,
            )
            if prefetch_context:
                prefetched = True
                # 稳定前缀模式下,"已预检索过、不要重复检索"这句约束由这里携带,
                # 而不是由系统提示词里的 [[if prefetched]] 段。放在参考内容之后、
                # 用户问题之前:它是对紧接着这段材料的使用说明。
                guidance = (
                    "\n\n[以上参考内容由系统预先检索得到。若已经足够，请直接回答、"
                    "不要重复检索；不足时再调用工具补充。]"
                    if settings.PROMPT_CACHE_STABLE_PREFIX
                    else ""
                )
                user_content = (
                    "[系统已预先从本地知识库检索到以下参考内容]\n"
                    + prefetch_context
                    + guidance
                    + "\n\n[用户问题]\n"
                    + prompt
                )
                if prefetch_citations:
                    yield {"type": "citations", "items": prefetch_citations}
            elif not prefetch_failed:
                # 预检索跑了但没查到。不说这件事的话,模型看到的和"没做预检索"
                # 完全一样,于是很可能用几乎相同的查询再检索一次——那一轮必然
                # 也是空的。轨迹里记了这次查询,但那是给**下一回合**回灌的,
                # 本回合的 messages 里没有。
                #
                # 放在用户消息里而不是加一个模板开关:五个提示词版本都要改才能
                # 让开关生效,而这条信息本身是"本轮发生了什么"——和预检索命中时
                # 注入参考内容是同一件事,理应走同一条路。
                user_content = (
                    f"[系统已用「{search_query[:200]}」预先检索过本地知识库，"
                    "没有找到达到相关度要求的内容。若这个问题确实需要知识库资料，"
                    "请换一种说法重新检索，或先调用 list_knowledge_documents "
                    "看有哪些文档；若本来就不需要，直接作答。]\n\n[用户问题]\n"
                    + prompt
                )

        # ---- 跨会话长期记忆 ----
        # 独立的 system 消息而不是拼进主系统提示词:提示词是带版本管理的"代码",
        # 记忆是逐用户增长的"数据",混在一起会让同一版提示词在不同用户间
        # 表现不可比,也破坏语义缓存按 prompt_ref 分桶的前提。
        memory_messages: list[dict[str, Any]] = []
        if settings.MEMORY_ENABLED:
            memory_block = memory_service.build_system_block(db, user_id)
            if memory_block:
                memory_messages = [{"role": "system", "content": memory_block}]

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._system_prompt(system_template, prefetched),
            },
            *memory_messages,
            *context.messages,
            # 轨迹紧贴当前问题:它讲的是"刚刚做过什么",离问题越近越不容易被
            # 当成更早的对话内容
            *trajectory,
            {"role": "user", "content": self._user_content(user_content, turn, model)},
        ]

        # ---- 显式规划(plan-and-execute) ----
        # 位置是有讲究的:必须在 messages 拼好之后、进循环之前。
        #   - 在预检索之后:计划要能看到"资料已经拿到了",否则它会规划一步去查
        #     已经在眼前的东西(见 prompts/agent_plan/ 里那条规则)
        #   - 在循环之前:计划是**事前**的全局视野,边跑边规划就退回 ReAct 了
        # 计划接在用户消息后面而不是塞进系统提示词:它每轮都不同,而系统提示词是
        # 整个前缀的第一条消息,让它随本轮内容变化就是把提示词缓存整段作废
        # (同 PROMPT_CACHE_STABLE_PREFIX 那段的理由)。
        plan: list[dict[str, Any]] = []
        if planner.enabled() and runtime.schemas:
            plan = await planner.build_plan(
                self.model_adapter,
                question=prompt,
                context=prefetch_context if prefetched else "",
                tool_names=[
                    schema["function"]["name"] for schema in runtime.schemas
                ],
            )
            # 空计划不发事件也不注入:模型判断"直接答就行"是正确输出,给前端推一个
            # 空计划卡片纯属噪声。步数进埋点,那样"规划到底有没有产出"是可查的
            # ——0 步和"规划挂了"在这里同形,靠 planner 里那条 warning 区分。
            turn.set(plan_steps=len(plan) or None)
            if plan:
                messages.append({"role": "user", "content": planner.format_steps(plan)})
                yield {"type": "plan", "steps": plan}
        # ---- 状态与协作者分离 ----
        # state 是能进数据库、能跨请求的那一半;context 是持有会话与客户端的那一半。
        # 改动之前两者混在同一批局部变量里,所以没有任何一个子集是可以存下来的。
        state = agent_state.TurnState(
            run_id=run_id,
            chat_id=chat_id,
            user_id=user_id,
            workspace_id=scope.workspace_id,
            is_admin=scope.is_admin,
            message_id=message_id,
            use_rag=use_rag,
            prompt=prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            prompt_ref=system_template.ref,
            delegation_mode=settings.AGENT_DELEGATION_MODE,
            plan=plan,
            messages=messages,
            budget_remaining=budget.remaining,
            budget_per_call=settings.TOOL_RESULT_MAX_CHARS,
        )
        ctx = _TurnContext(
            runtime=runtime,
            budget=budget,
            repeats=repeats,
            breaker=breaker,
            citations=citations,
            generation=generation,
            scope=scope,
            turn=turn,
            delegations=delegations,
            gated=frozenset(approval.gated_tools()),
        )
        if checkpoint_store.enabled():
            checkpoint_store.start_run(
                db,
                run_id=run_id,
                chat_id=chat_id,
                user_id=user_id,
                message_id=message_id,
                model=generation["model"],
                prompt_ref=system_template.ref,
                trace_id=current_trace_id(),
            )
            turn.set(run_id=run_id)
            # 先把 run id 发出去,再开始跑。
            #
            # 位置是刻意的:客户端要在**任何东西可能断掉之前**拿到这个 id,否则
            # 断线之后它没有接续凭证——原来只有 approval_required / clarification
            # 这两类事件带 runId,而那两件事恰好是"没断线"的情形。
            yield {
                "type": "run_started",
                "runId": run_id,
                "chatId": chat_id,
            }

        async for event in self._drive_loop(db, state, ctx):
            yield event

    async def _drive_loop(
        self,
        db: Session,
        state: agent_state.TurnState,
        ctx: _TurnContext,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Agent 主循环。状态全部读写 ``state``,协作者全部来自 ``ctx``。

        新回合与恢复走的是同一个函数,区别只在进来时 ``state`` 的内容:新回合是
        刚构造的(``pending_calls`` 空、``round_index`` 为 0),恢复是从快照读出来的
        (``pending_calls`` 非空、``pending_index`` 指向下一个要跑的调用)。
        这一点是整个设计的支点——如果恢复走另一条代码路径,两条路就会各自漂移,
        而漂移的表现是"恢复之后行为微妙地不一样",极难定位。
        """
        max_rounds = max(1, settings.AGENT_MAX_TOOL_ROUNDS)
        emitted_any = state.emitted_any
        force_final = state.force_final
        round_index = state.round_index
        # 恢复进来时本轮的工具还没跑完:跳过模型调用,直接进工具执行段。
        resuming = bool(state.pending_calls) and state.pending_index < len(
            state.pending_calls
        )
        turn = ctx.turn
        runtime = ctx.runtime
        budget = ctx.budget
        repeats = ctx.repeats
        citations = ctx.citations
        delegations = ctx.delegations
        generation = ctx.generation
        # 同一个列表对象:往它 append 就是往 state.messages append。
        # 这是刻意的——循环里到处都在追加消息,每次都写 state.messages 会让
        # 这段代码比改动前难读,而收益是零(反正是同一个对象)。
        messages = state.messages

        while True:
            if not resuming:
                round_index += 1
                state.round_index = round_index
                # 本轮内创建的所有 span(模型调用、工具、检索)都会自动带上轮次
                set_span_defaults(round=round_index)
                turn.set(rounds=round_index)
                # 最后一轮不再下发工具 schema:模型只能作答,循环必然终止。
                is_final_round = force_final or round_index >= max_rounds
                if is_final_round and round_index > 1:
                    messages.append(
                        {
                            "role": "user",
                            "content": "[系统提示] 工具调用阶段已结束，请基于当前已获得的信息"
                            "给出最终回答，不要再尝试调用任何工具。",
                        }
                    )
                    yield {"type": "tool_rounds_ended", "rounds": round_index - 1}

                tools_for_round = [] if is_final_round else runtime.schemas
                completion: ModelCompletion | None = None
                round_streamed_text = False
                try:
                    async for chunk in self.model_adapter.stream_completion(
                        messages=messages, tools=tools_for_round, **generation
                    ):
                        if chunk.text:
                            emitted_any = True
                            round_streamed_text = True
                            yield {"type": "message_delta", "content": chunk.text}
                        elif chunk.completion is not None:
                            completion = chunk.completion
                except Exception as exc:
                    logger.error(
                        "stream_completion failed on round %s: %s",
                        round_index,
                        type(exc).__name__,
                    )
                    completion = None

                if completion is None:
                    # 已经流出过文本就不能重来,否则用户会看到重复内容。
                    if round_streamed_text:
                        self._finish_run(db, state, status="failed", error="stream_broken")
                        return
                    completion = await self._complete_fallback(
                        messages, tools_for_round, generation
                    )
                    if completion is None:
                        self._finish_run(db, state, status="failed", error="model_failed")
                        yield {"type": "error", "error": "模型调用失败，请稍后重试。"}
                        return

                if completion.protocol_error:
                    self._finish_run(db, state, status="failed", error="protocol_error")
                    yield {"type": "error", "error": completion.protocol_error}
                    return

                pending_calls = [] if is_final_round else completion.tool_calls
                if not pending_calls:
                    # 适配器已在流式阶段透出前 streamed_length 个字符,只补发剩余部分。
                    remainder = completion.content[completion.streamed_length :]
                    if remainder.strip():
                        yield {"type": "message_delta", "content": remainder}
                        self._finish_run(db, state, status="done")
                        return
                    if emitted_any:
                        self._finish_run(db, state, status="done")
                        return
                    # 不把空输出当成一条成功的 assistant 消息,给用户可见的错误提示。
                    self._finish_run(db, state, status="failed", error="empty_answer")
                    yield {"type": "error", "error": "模型未返回最终回答，请稍后重试。"}
                    return

                # ---- 执行本轮工具调用,把结果回灌 messages 后进入下一轮 ----
                messages.append(completion.as_assistant_message())
                state.uses_text_protocol = completion.uses_text_tool_protocol
                state.text_results = []
                state.pending_calls = [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in pending_calls
                ]
                state.pending_index = 0
                state.writes = []
                state.phase = "pre_tools"
                state.emitted_any = emitted_any
                state.force_final = force_final
                state.streamed_text = round_streamed_text
                if round_streamed_text:
                    state.streamed_prefix += completion.content[
                        : completion.streamed_length
                    ]
                # 本轮的快照。位置是刻意的:模型已经说完、工具一个都还没跑,
                # 此刻本轮没有任何面向用户的输出,是唯一能安全回到的点。
                ctx.sync_to(state)
                checkpoint_store.put(db, state)
            else:
                # 恢复路径:本轮的模型调用早就做完了,pending_calls 从快照来。
                pending_calls = [
                    ToolCall(
                        id=str(raw.get("id") or ""),
                        name=str(raw.get("name") or ""),
                        arguments=str(raw.get("arguments") or "{}"),
                    )
                    for raw in state.pending_calls
                ]
                is_final_round = force_final or round_index >= max_rounds
                resuming = False

            # 本轮有几次调用什么新东西都没带回来(工具挂了,或者是重复调用)。
            # 全都是的话继续循环只会原地转圈,下面据此强制收敛。
            barren_count = 0

            # 从 pending_index 起跑:恢复时前面那几个已经执行过了,它们的结果
            # 由 replay_writes 摆回了 messages。重跑一遍就是让那几次检索的钱
            # 白花第二遍,而结果不会更新——这是幂等性的全部所在。
            start_index = state.pending_index
            for call_index, call in enumerate(
                pending_calls[start_index:], start=start_index
            ):
                try:
                    arguments = json.loads(call.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}

                # ---- 人工审批闸门 ----
                # 位置是刻意的:在 tool_start 之前。发了 tool_start 再中断,界面上
                # 会留一个永远等不到 tool_result 的"正在执行",而它其实一步都没跑。
                call_key = call.id or f"r{round_index}c{call_index}"
                # 澄清用**轮次限定**的键，不复用 call_key。
                #
                # call_key 优先用 call.id，而"provider 的 call id 跨轮唯一"只是个
                # 假设：测试替身就给每轮的第 0 个调用都发 call-0。审批不受影响
                # （一次裁决消费掉就终止了），但澄清会在第 2 轮再问一次——那时
                # 第 1 轮的答案会顶替掉新问题，第二个问题永远问不出来。
                # 真实 provider 大多确实唯一，但这条路径没有理由依赖它。
                ask_key = f"r{round_index}c{call_index}"
                rejected_result: ToolResult | None = None
                # 用户改过参数的话，在这里把它写回 call。位置是刻意的：
                # **在校验和执行之前，在闸门判定之前**。
                #
                # 写回 ``call.arguments``（字符串）而不是只改下面那个 dict：
                # ``ToolRuntime.execute`` 校验和执行读的都是这个字符串，只改 dict
                # 的话跑起来还是模型原来那份参数——而界面上会显示用户改过的值。
                edited_note: list[str] = []
                if call_key in state.edited_arguments:
                    raw_edited = state.edited_arguments[call_key]
                    try:
                        parsed_edited = json.loads(raw_edited)
                    except json.JSONDecodeError:
                        parsed_edited = None
                    if isinstance(parsed_edited, dict):
                        edited_note = sorted(
                            key
                            for key in parsed_edited
                            if parsed_edited.get(key) != arguments.get(key)
                        )
                        arguments = parsed_edited
                        call.arguments = raw_edited
                if call.name in ctx.gated:
                    if call_key in state.rejected_call_ids:
                        rejected_result = ToolResult(
                            approval.rejection_message(call.name, state.interrupt_note),
                            ToolStatus.REJECTED,
                        )
                    elif call_key not in state.approved_call_ids:
                        # 停在这里，把状态落库，然后让这个请求结束。
                        # 恢复发生在另一个 HTTP 请求里——这正是快照存在的理由。
                        ctx.sync_to(state)
                        state.phase = "waiting_approval"
                        state.status = "waiting_approval"
                        state.pending_index = call_index
                        state.emitted_any = emitted_any
                        state.force_final = force_final
                        request = agent_state.InterruptRequest(
                            kind="tool_approval",
                            tool=call.name,
                            arguments=arguments,
                            call_index=call_index,
                            tool_call_id=call.id,
                            reason=approval.reason_for(call.name),
                        )
                        state.interrupt = request.to_dict()
                        seq = checkpoint_store.put(db, state, interrupt=state.interrupt)
                        checkpoint_store.update_run(
                            db,
                            state.run_id,
                            status="waiting_approval",
                            rounds=round_index,
                            bump_interrupts=True,
                        )
                        # 审计留痕。位置是刻意的：在 approval_required 发出去
                        # **之前**。反过来的话，进程在这两步之间挂掉就会留下一个
                        # 用户看到过、但审计里不存在的审批请求。
                        approval_audit.record_request(
                            db,
                            run_id=state.run_id,
                            chat_id=state.chat_id,
                            user_id=state.user_id,
                            tool_name=call.name,
                            tool_call_id=call.id,
                            round_index=round_index,
                            call_index=call_index,
                            arguments=arguments,
                            reason=request.reason,
                        )
                        turn.set(interrupted="tool_approval", interrupt_tool=call.name)
                        yield {
                            "type": "approval_required",
                            "runId": state.run_id,
                            "tool": call.name,
                            "preview": approval.build_preview(arguments),
                            "reason": request.reason,
                            "round": round_index,
                            "checkpoint": seq,
                        }
                        return

                yield {
                    "type": "tool_start",
                    "tool": call.name,
                    "input": arguments,
                    "round": round_index,
                }
                # 委派要等一整个子代理循环跑完才返回,几十秒不出声的话界面上
                # 只有一个转圈。先说清正在等谁。
                if call.name == "delegate":
                    yield {
                        "type": "agent_state",
                        "agent": str(arguments.get("role") or "?"),
                        "status": "started",
                        "round": round_index,
                    }
                async with tracer.span(
                    f"tool.{call.name}", SpanKind.TOOL, arguments=len(arguments) or None
                ) as tool_span:
                    # 被用户拒绝的调用在这里短路,连重复检测都不走:它不是"又调了
                    # 一次",而是"这一次不许调"。也因此它绝不碰熔断器——用户拒绝
                    # 一次不代表工具坏了,熔断掉会让他改主意之后反而调不动。
                    if rejected_result is not None:
                        result = rejected_result
                        reports = []
                        tool_span.set(rejected=True)
                        tool_span.status = "error"
                        tool_span.error_type = "user_rejected"
                    # 重复检测放在执行之前:被拦下的调用不该真的跑一遍。它也必须
                    # 在 span 之内——"这一步被拦了"和"这一步失败了"一样是轨迹的
                    # 一部分,漏在 span 外面的话 trace 里会缺一步,轮次对不上。
                    else:
                        result = repeats.check(call.name, arguments)
                        if result is None:
                            with guardrails.collecting() as reports:
                                result = await runtime.execute(call)
                        else:
                            # 没执行就没有护栏可收。给个空列表让下面的汇总逻辑统一。
                            reports = []
                            tool_span.set(repeated=True)
                    tool_span.set(result_status=result.status.value)
                    if result.status is not ToolStatus.OK:
                        # 工具失败要能在 trace 里直接筛出来,而不是埋在 attributes 里
                        tool_span.status = "error"
                        tool_span.error_type = result.status.value
                # 子代理的步骤在它自己那次 delegate 返回之后才拿得到(理由见
                # _Delegations 的文档串)。这里先把它们发出去并落库,再发外层
                # delegate 的 tool_result——顺序反了的话界面上会先看到"委派完成",
                # 然后才冒出它做过的那几步。
                if delegations is not None and delegations.pending:
                    for outcome in delegations.take():
                        async for event in self._emit_subagent_steps(
                            db,
                            outcome,
                            state=state,
                            round_index=round_index,
                        ):
                            yield event
                yield {
                    "type": "tool_result",
                    "tool": call.name,
                    "status": result.status.value,
                    "round": round_index,
                }
                tool_report = guardrails.summarize(reports)
                if tool_report is not None:
                    yield self._guardrail_event(tool_report, round_index=round_index)
                # 引用先取一份再发:sink 发完就清空,而落库要用同一批
                step_citations = list(citations)
                if citations:
                    yield {"type": "citations", "items": step_citations}
                    citations.clear()
                # 被拒绝的调用也算"没带回新东西":它确实没有。不算的话,模型下一轮
                # 很可能把同一件事换个说法再提一次,而人已经说过不同意了。
                if result.status in (
                    ToolStatus.UNAVAILABLE,
                    ToolStatus.REPEATED,
                    ToolStatus.REJECTED,
                ):
                    barren_count += 1

                # 存的是预算裁剪之前的原文。预算约束的是"这一回合往上下文塞多少",
                # 不该顺手决定"以后还能回看多少"。
                tool_history.record(
                    db,
                    chat_id=state.chat_id,
                    message_id=state.message_id,
                    round_index=round_index,
                    call_index=call_index,
                    tool_name=call.name,
                    status=result.status.value,
                    result=result.content,
                    tool_call_id=call.id,
                    arguments=arguments,
                    citations=step_citations,
                    run_id=state.run_id,
                )

                # 澄清工具:回合在这里停下,等用户回答。
                # 非 OK 状态(参数校验不过)走正常回灌路径,让模型自己改。
                if (
                    call.name == "ask_user"
                    and result.status is ToolStatus.OK
                    # 已经答过的那次不再挂起：否则恢复时又停在同一个问题上，
                    # 永远走不到下一轮。答案在下面以 role=tool 回灌。
                    and ask_key not in state.clarification_answers
                ):
                    turn.set(clarification=True)
                    question = result.content.strip()
                    # 开了 checkpoint 就**挂起**,等答案回来接着这一轮跑;
                    # 没开就只能像原来那样收尾——没有快照,答案回来时无处可接,
                    # 用户那句话只能变成全新一轮(前面几轮的工具结果全丢)。
                    if checkpoint_store.enabled():
                        ctx.sync_to(state)
                        state.phase = "waiting_input"
                        state.status = "waiting_input"
                        state.pending_index = call_index
                        state.emitted_any = emitted_any
                        state.force_final = force_final
                        request = agent_state.InterruptRequest(
                            kind="user_input",
                            tool=call.name,
                            arguments={"question": question},
                            call_index=call_index,
                            tool_call_id=call.id,
                            reason=question,
                        )
                        state.interrupt = request.to_dict()
                        seq = checkpoint_store.put(
                            db, state, interrupt=state.interrupt
                        )
                        checkpoint_store.update_run(
                            db,
                            state.run_id,
                            status="waiting_input",
                            rounds=round_index,
                            bump_interrupts=True,
                        )
                        turn.set(interrupted="user_input")
                        yield {
                            "type": "clarification",
                            "runId": state.run_id,
                            "question": question,
                            "round": round_index,
                            "checkpoint": seq,
                            # 告诉前端这次是可续的。缺这个键的 clarification 事件
                            # 是旧行为(答案变成新一轮),前端据此决定走哪条路
                            "resumable": True,
                        }
                        return
                    self._finish_run(db, state, status="done", rounds=round_index)
                    yield {
                        "type": "clarification",
                        "question": question,
                        "round": round_index,
                    }
                    return

                # 澄清的答案顶替工具结果本身。ask_user 的"结果"是它自己那句问题,
                # 回灌问题毫无意义——模型要的是答案。
                if ask_key in state.clarification_answers:
                    result = ToolResult(
                        f"用户回答：{state.clarification_answers[ask_key]}",
                        ToolStatus.OK,
                    )
                content = budget.take(result.content)
                # 参数被人改过就在结果前面说清楚。不说的话模型会照自己原来那份参数
                # 向用户复述——用户刚把标题改成"Q3 复盘"，模型还在说"已保存《季度
                # 总结草稿》"。放在结果**前面**是因为后面那段可能被预算裁掉。
                if edited_note and result.status is ToolStatus.OK:
                    content = (
                        approval.edit_message(
                            call.name, edited_note, state.interrupt_note
                        )
                        + "\n"
                        + content
                    )
                if state.uses_text_protocol:
                    state.text_results.append(f"工具 {call.name} 的结果：\n{content}")
                else:
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": content}
                    )

                # 这一步做完了。记一条 write 并把游标往前挪一格——如果本轮后面
                # 某个调用要审批,快照就带着这条记录停下,恢复时它被摆回 messages
                # 而**不重新执行**。这是幂等性的全部来源。
                repeat_counts, repeat_blocked = repeats.snapshot()
                breaker_consecutive, breaker_tripped = ctx.breaker.snapshot()
                state.writes.append(
                    agent_state.make_write(
                        index=call_index,
                        call_id=call.id,
                        name=call.name,
                        status=result.status.value,
                        content=content,
                        budget_after=budget.remaining,
                        repeat_counts=repeat_counts,
                        repeat_blocked=repeat_blocked,
                        breaker_consecutive=breaker_consecutive,
                        breaker_tripped=breaker_tripped,
                        citations=step_citations,
                    )
                )
                state.pending_index = call_index + 1

            if state.uses_text_protocol:
                messages.append(
                    {
                        "role": "user",
                        "content": "以下是已执行工具的内部结果。请据此继续，"
                        "必要时可再次调用工具；不要展示工具调用标记或工具协议。\n\n"
                        + "\n\n".join(state.text_results),
                    }
                )

            # 本轮每次调用都没带回新东西(工具全不可用,或全是重复调用)时,继续
            # 循环只会原地转圈;结果预算耗尽同理。
            if barren_count == len(pending_calls) or budget.exhausted:
                force_final = True
            if repeats.blocked:
                # 埋在 turn 上而不是逐次记:关心的是"这次回答里模型转了几圈",
                # 一个数就够,而它要能和轮次、成本放在同一行里看。
                turn.set(repeated_blocked=repeats.blocked)

            # 本轮收尾:把游标清零,下一轮重新填。不清的话下一轮进来时
            # pending_index 还指着上一轮的末尾,新一批工具会被整批跳过。
            state.phase = "post_tools"
            state.pending_calls = []
            state.pending_index = 0
            state.writes = []
            state.text_results = []
            state.emitted_any = emitted_any
            state.force_final = force_final
            ctx.sync_to(state)
            checkpoint_store.update_run(
                db,
                state.run_id,
                rounds=round_index,
                delegations=state.delegations_used,
            )
            # 轮次边界的快照。这是**断线之后唯一能安全回到的点**。
            #
            # 为什么不能从 pre_tools 那份恢复:那份是在"模型说完、工具还没跑"时拍的,
            # 从它恢复会把本轮的工具**重跑一遍**。只读工具重跑是白花钱,而
            # save_to_knowledge_base 重跑就是写第二份——审批那条路径靠 writes +
            # replay_writes 精确避开了这件事,但断线时 writes 停在快照那一刻,
            # 之后真正跑掉的那几步没人记下来,所以那套机制在这里用不上。
            #
            # 到了 post_tools 就没有这个问题:本轮工具全部执行完,结果已经在
            # state.messages 里,恢复等于"接着发下一轮的模型调用"。
            #
            # 代价是每轮多一份快照(一份就是整个 messages,几十 KB),由
            # AGENT_CHECKPOINT_KEEP 兜着——它本来就是为这个存在的。
            checkpoint_store.put(db, state)

    @staticmethod
    def _finish_run(
        db: Session,
        state: agent_state.TurnState,
        *,
        status: str,
        error: str | None = None,
        rounds: int | None = None,
    ) -> None:
        """给执行记录盖章。

        每条 ``return`` 路径都要走这里,否则那一行会永远停在 ``running``——
        而"还在跑"和"跑挂了"在待恢复列表里是完全不同的意思。
        """
        state.status = status  # type: ignore[assignment]
        checkpoint_store.update_run(
            db,
            state.run_id,
            status=status,
            rounds=rounds,
            error_type=error,
            finished=True,
        )

    async def answer_clarification(
        self,
        db: Session,
        user_id: str,
        run_id: str,
        *,
        answer: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """带着用户的回答，从 ``ask_user`` 处**接着这一轮**跑下去。

        和审批恢复共用全部机制（快照、重建工具面、拨回余额），区别只有一处：
        回灌的东西不是"执行了/没执行"，而是用户写的那句话，且它必须以
        ``role=tool`` 接在模型那次 ask_user 调用后面。

        ## 为什么不能让答案变成新一轮

        改动之前 ask_user 是终止回合的：模型问完，run 落 ``done``，用户的回答
        作为新消息开一轮全新的。后果是**前面几轮的工具结果全部丢掉**——模型
        检索了三次、读了两个文档，然后问了一句"你要哪个季度的"，用户答完
        它得从零再来。对话历史里还留着那些内容，但工具结果不在历史里，
        它们活在上一轮的 ``messages`` 里，而那个列表已经跟着生成器一起没了。

        接着跑的话，答案落在原来那批 ``messages`` 的末尾，模型手里还有它刚
        检索到的一切。这是"问清楚再动手"和"猜错了重来"的区别。
        """
        run = checkpoint_store.get_run(db, run_id)
        if run is None or run.user_id != user_id:
            yield {"type": "error", "error": "找不到这次执行，或它不属于当前用户。"}
            return
        if run.status != "waiting_input":
            yield {
                "type": "error",
                "error": f"这次执行当前状态是 {run.status}，不在等待回答。",
            }
            return

        state = checkpoint_store.latest(db, run_id)
        if state is None or state.interrupt is None:
            yield {"type": "error", "error": "这次执行的状态快照已不可用，无法恢复。"}
            return
        request = state.interrupt_request
        if request is None or request.kind != "user_input":
            yield {"type": "error", "error": "中断请求已损坏，无法恢复。"}
            return

        cleaned = answer.strip()
        if not cleaned:
            # 空回答不该消耗这次中断：run 留在 waiting_input，用户可以再答一次。
            # 放过去的话模型收到一条空的 tool 消息，只能再问一遍或者开始猜。
            yield {"type": "error", "error": "回答不能为空。"}
            return

        # 用户的原话要过 mask_markup 再进 messages。它会以 role=tool 的身份出现，
        # 而模型对 tool 内容的信任度比 user 更高——这个位置更值得防注入，
        # 不是更不值得。
        # 键必须和循环里的 ``ask_key`` 一致：轮次 + 下标，不用 tool_call_id
        # （理由见那边的注释）。
        state.clarification_answers = {
            **state.clarification_answers,
            f"r{state.round_index}c{request.call_index}": (
                guardrails.mask_markup(cleaned)[: settings.TOOL_RESULT_MAX_CHARS]
            ),
        }
        state.interrupt = None
        state.phase = "pre_tools"
        state.status = "running"
        checkpoint_store.update_run(db, run_id, status="running")

        yield {
            "type": "clarification_answered",
            "runId": run_id,
            "round": state.round_index,
        }

        async for event in self._resume_loop(db, state, request, True):
            yield event

    async def resume_turn(
        self,
        db: Session,
        user_id: str,
        run_id: str,
        *,
        approved: bool,
        note: str = "",
        edited_arguments: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """从审批中断处恢复一次执行。

        这个方法是整套改动的目的所在：它跑在**另一个 HTTP 请求**里，原来那条 SSE
        连接早就断了，驱动它的那个异步生成器早就被回收了。能接上，是因为状态在
        数据库里而不在进程里。

        三件事必须做对，少一件恢复就是"重新开始"：

        1. **重建的工具面要和中断前一致。** 走 ``_build_tool_surface``，与新回合
           同一个函数（理由见那边的文档串）。
        2. **守卫余额要拨回中断那一刻。** ``replay_writes`` + ``restore_from``。
           少了这一步，用户点一次同意就等于给模型重发一份预算、清空重复计数、
           复活被熔断的工具。
        3. **已经执行过的工具不能再执行。** 同样由 ``replay_writes`` 保证：它把
           那几步的结果摆回 ``messages``，而不是让它们重跑一遍。
        """
        run = checkpoint_store.get_run(db, run_id)
        if run is None or run.user_id != user_id:
            yield {"type": "error", "error": "找不到这次执行，或它不属于当前用户。"}
            return
        if run.status != "waiting_approval":
            # 幂等：同一个审批被点两次（双击、两个标签页）时第二次走到这里。
            # 报一句明确的话，而不是让它再跑一遍——那会把已批准的写操作执行两次。
            yield {
                "type": "error",
                "error": f"这次执行当前状态是 {run.status}，不在等待审批。",
            }
            return

        state = checkpoint_store.latest(db, run_id)
        if state is None or state.interrupt is None:
            yield {"type": "error", "error": "这次执行的状态快照已不可用，无法恢复。"}
            return

        request = state.interrupt_request
        if request is None:
            yield {"type": "error", "error": "中断请求已损坏，无法恢复。"}
            return

        # 裁决只对**这一次调用**生效。用 call_id（缺失时用轮次+下标）而不是工具名：
        # 按工具名放行等于"以后这个工具都不用问了"，那审批就只剩第一次有意义。
        call_key = request.tool_call_id or f"r{state.round_index}c{request.call_index}"
        changed_keys: list[str] = []
        if approved and edited_arguments is not None:
            # 只能改**这次调用的参数**，工具名不可改。放开工具名等于让审批弹窗
            # 变成越权通道：用户看到并同意的是"保存到知识库"，改成"删除文档"
            # 就成了拿一次同意换另一件事。
            merged, error = approval.validate_edit(request.arguments, edited_arguments)
            if merged is None:
                yield {"type": "error", "error": f"参数修改无效：{error}"}
                return
            changed_keys = sorted(
                key
                for key in merged
                if merged.get(key) != request.arguments.get(key)
            )
            state.edited_arguments = {
                **state.edited_arguments,
                call_key: json.dumps(merged, ensure_ascii=False),
            }
            # 改完的参数**不再回到闸门**。人刚刚亲手写了这些值，再弹一次让他确认
            # 自己写的东西，是在训练无脑点确认——而那正是审批失效的方式。
        if approved:
            state.approved_call_ids = [*state.approved_call_ids, call_key]
        else:
            state.rejected_call_ids = [*state.rejected_call_ids, call_key]
        state.interrupt_note = note
        state.interrupt = None
        state.phase = "pre_tools"
        state.status = "running"
        checkpoint_store.update_run(db, run_id, status="running")
        # 审计留痕：谁批的、什么时候、以及**真正要执行的那份参数**。
        # effective_arguments 传改后的那份（没改就是原始那份），它的 digest
        # 与请求时的不同就说明执行的不是模型原本要执行的东西。
        effective = request.arguments
        if changed_keys:
            effective = json.loads(state.edited_arguments[call_key])
        approval_audit.record_decision(
            db,
            run_id=run_id,
            decided_by=user_id,
            approved=approved,
            note=note,
            effective_arguments=effective,
            edited_fields=changed_keys,
        )

        resolved: dict[str, Any] = {
            "type": "approval_resolved",
            "runId": run_id,
            "tool": request.tool,
            "approved": approved,
            "round": state.round_index,
        }
        # 只在真的改过时才带这个键：空列表和"没编辑"在界面上是两件事，
        # 而恒在的 "edited": [] 会被读成"编辑过但什么都没改"。
        if changed_keys:
            resolved["edited"] = changed_keys
        yield resolved

        async for event in self._resume_loop(db, state, request, approved):
            yield event

    async def _resume_loop(
        self,
        db: Session,
        state: agent_state.TurnState,
        request: agent_state.InterruptRequest,
        approved: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """重建协作者，把状态拨回中断那一刻，然后交给主循环。"""
        # 作用域从快照来，不重新解析：中断期间用户的 workspace 或角色理论上可能
        # 变过，但那时候正确的做法是让这次恢复失败，而不是拿新权限接着跑一个
        # 在旧权限下批准的操作。
        scope = _ToolScope(
            user_id=state.user_id,
            workspace_id=state.workspace_id,
            is_admin=state.is_admin,
        )
        async with tracer.trace(
            user_id=state.user_id, chat_id=state.chat_id, message_id=state.message_id
        ):
            async with tracer.span(
                "chat.resume",
                SpanKind.AGENT,
                model=state.model or settings.LLM_MODEL,
                run_id=state.run_id,
                resumed_from=state.round_index,
                approved=approved,
                prompt_version=state.prompt_ref,
            ) as turn:
                history = await self._get_chat_history_messages(
                    db, state.chat_id, exclude_message_id=state.message_id
                )
                scope.history = history
                # 确认令牌：用户在界面上点的那一下，比词表扫出来的"用户说过删除"
                # 是更强的证据。所以这里不再扫原话，直接按裁决给——但**只在**
                # 被批准的那次调用确实是删除操作时给，而不是整回合放开。
                approvals = workspace_tools._ToolApprovals(
                    delete_granted=(
                        approved and request.tool == "delete_knowledge_document"
                    )
                )
                citations: list[dict] = []
                budget = _ToolResultBudget(
                    settings.TOOL_RESULT_TOTAL_CHARS, settings.TOOL_RESULT_MAX_CHARS
                )
                breaker = CircuitBreaker(settings.TOOL_CIRCUIT_BREAKER_FAILURES)
                repeats = RepeatGuard(settings.AGENT_REPEAT_LIMIT)
                generation: dict[str, Any] = {
                    "model": state.model or settings.LLM_MODEL,
                    "temperature": state.temperature,
                    "max_tokens": state.max_tokens,
                    "top_p": state.top_p,
                }
                base_tools = self._create_tools(
                    db, scope, state.use_rag, citations, approvals
                )
                runtime, delegations = self._build_tool_surface(
                    base_tools,
                    generation=generation,
                    take_budget=budget.take,
                    breaker=breaker,
                    turn=turn,
                )
                ctx = _TurnContext(
                    runtime=runtime,
                    budget=budget,
                    repeats=repeats,
                    breaker=breaker,
                    citations=citations,
                    generation=generation,
                    scope=scope,
                    turn=turn,
                    delegations=delegations,
                    gated=frozenset(approval.gated_tools()),
                )
                # 顺序要紧：先 replay（它会按 writes 把余额算到中断那一刻），
                # 再 restore（把算出来的余额灌进守卫对象）。反过来就白算了。
                agent_state.replay_writes(state)
                state.pending_index = request.call_index
                ctx.restore_from(state)
                turn.set(replayed_writes=len(state.writes))

                async for event in self._drive_loop(db, state, ctx):
                    yield event

    async def continue_orphan(
        self,
        db: Session,
        user_id: str,
        run_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """接上一个因断线而没跑完的回合。

        与 ``resume_turn`` 的区别在于**没有裁决要消费**:没有人做错什么,是连接断了。
        所以这里不碰 ``approved_call_ids``、不重建确认令牌、也不发
        ``approval_resolved``。

        **只从 ``post_tools`` 的快照接。** 这是整件事的支点:那个 phase 意味着本轮
        工具全部执行完、结果已经在 ``messages`` 里,所以恢复等于"接着发下一轮的
        模型调用",一次工具都不会重跑。

        从 ``pre_tools`` 接是不安全的——那份快照拍在"模型说完、工具还没跑"的位置,
        接上去会把本轮工具重跑一遍。审批那条路径能安全地从 pre_tools 接,靠的是
        ``writes`` 里逐步记着"哪几个已经跑完了";而断线时 ``writes`` 停在拍快照
        那一刻,之后真正跑掉的那几步没人记下来,所以那套机制在这里用不上。
        只读工具重跑是白花钱,``save_to_knowledge_base`` 重跑就是写第二份。
        """
        run = checkpoint_store.get_run(db, run_id)
        if run is None or run.user_id != user_id:
            yield {"type": "error", "error": "找不到这次执行，或它不属于当前用户。"}
            return
        if run.status != "running":
            yield {
                "type": "error",
                "error": f"这次执行当前状态是 {run.status}，不是断线未完成。",
            }
            return

        state = checkpoint_store.latest(db, run_id)
        if state is None:
            yield {"type": "error", "error": "这次执行没有可用的状态快照，无法接续。"}
            return
        if state.phase != "post_tools":
            # 停在 pre_tools 说明断线发生在工具执行**当中**。那一轮里哪几个工具
            # 真的跑完了没有记录,接上去要么重跑(可能重复写入)、要么跳过
            # (模型看不到本该有的结果)。两者都比"请重新提问"更糟。
            yield {
                "type": "error",
                "error": (
                    "这次执行断在工具执行中途，无法安全接续（可能造成重复写入）。"
                    "请重新提问。"
                ),
            }
            checkpoint_store.update_run(
                db, run_id, status="failed", error_type="unsafe_resume_point"
            )
            return

        scope = _ToolScope(
            user_id=state.user_id,
            workspace_id=state.workspace_id,
            is_admin=state.is_admin,
        )
        checkpoint_store.update_run(db, run_id, status="running")
        yield {"type": "run_resumed", "runId": run_id, "round": state.round_index}

        async with tracer.trace(
            user_id=state.user_id, chat_id=state.chat_id, message_id=state.message_id
        ):
            async with tracer.span(
                "chat.continue",
                SpanKind.AGENT,
                model=state.model or settings.LLM_MODEL,
                run_id=state.run_id,
                resumed_from=state.round_index,
                prompt_version=state.prompt_ref,
            ) as turn:
                history = await self._get_chat_history_messages(
                    db, state.chat_id, exclude_message_id=state.message_id
                )
                scope.history = history
                # 删除令牌不给:它的依据是"用户原话里要求过",而这里没有任何
                # 新的用户输入。断线不该顺带把一次删除授权带过来。
                approvals = workspace_tools._ToolApprovals(delete_granted=False)
                citations: list[dict] = []
                budget = _ToolResultBudget(
                    settings.TOOL_RESULT_TOTAL_CHARS, settings.TOOL_RESULT_MAX_CHARS
                )
                breaker = CircuitBreaker(settings.TOOL_CIRCUIT_BREAKER_FAILURES)
                repeats = RepeatGuard(settings.AGENT_REPEAT_LIMIT)
                generation: dict[str, Any] = {
                    "model": state.model or settings.LLM_MODEL,
                    "temperature": state.temperature,
                    "max_tokens": state.max_tokens,
                    "top_p": state.top_p,
                }
                base_tools = self._create_tools(
                    db, scope, state.use_rag, citations, approvals
                )
                runtime, delegations = self._build_tool_surface(
                    base_tools,
                    generation=generation,
                    take_budget=budget.take,
                    breaker=breaker,
                    turn=turn,
                )
                ctx = _TurnContext(
                    runtime=runtime,
                    budget=budget,
                    repeats=repeats,
                    breaker=breaker,
                    citations=citations,
                    generation=generation,
                    scope=scope,
                    turn=turn,
                    delegations=delegations,
                    gated=frozenset(approval.gated_tools()),
                )
                # 守卫余额直接从快照灌:post_tools 的快照里 writes 已经清空
                # (本轮收尾时清的),而余额、重复计数、熔断状态都是那一刻的真值。
                # 不能像审批那条路径一样调 replay_writes——它会按空的 writes
                # 把 pending_index 拨成 0,而这里本来就该是 0。
                ctx.restore_from(state)
                turn.set(resumed_round=state.round_index)

                async for event in self._drive_loop(db, state, ctx):
                    yield event

    async def generate_ai_response(
        self,
        db: Session,
        user_id: str,
        chat_id: str,
        prompt: str,
        model: str | None = None,
        use_rag: bool = False,
        message_id: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
        prompt_version: str | None = None,
    ) -> str:
        """非流式接口,复用相同的工具运行时逻辑。"""
        response_parts: list[str] = []
        async for event in self.stream_ai_response(
            db,
            user_id,
            chat_id,
            prompt,
            model,
            use_rag,
            message_id,
            temperature,
            max_tokens,
            top_p,
            prompt_version,
        ):
            if event["type"] == "message_delta":
                response_parts.append(event["content"])
            elif event["type"] == "error":
                raise RuntimeError(event["error"])
        return "".join(response_parts)
