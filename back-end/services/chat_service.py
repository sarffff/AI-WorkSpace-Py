import json
import logging
from typing import Any, AsyncGenerator

from sqlalchemy.orm import Session

from config import settings
from models import Chat, Message, MessageToolStep
from redis_service import redis_service
from services.clock import naive_now
from services.conversation_context import ConversationContextBuilder
from services.feedback_service import feedback_service
from services import guardrails
from services.guardrails import guard, mask_markup
from services.knowledge_service import KnowledgeService
from services.model_adapter import ModelAdapter, ModelCompletion, OpenAICompatibleAdapter
from services import prompt_library
from services.semantic_cache import semantic_cache
from services.telemetry import SpanKind, set_span_defaults, tracer
from services.token_budget import HistoryMessage
from services import agent_roles
from services import subagent
from services import tool_history
from services.tool_runtime import ToolDefinition, ToolRuntime, ToolStatus
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

    def take(self, text: str) -> str:
        limit = min(self._per_call, self._remaining)
        if limit <= 0:
            return "[上下文预算已用尽，工具结果未注入。请基于已获得的信息直接回答。]"
        if len(text) <= limit:
            self._remaining -= len(text)
            return text
        self._remaining = 0
        return text[:limit] + f"\n\n[结果过长已截断，原始长度 {len(text)} 字符]"


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
        """
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
        user_id: str,
        use_rag: bool,
        citation_sink: list[dict] | None = None,
    ) -> list[ToolDefinition]:
        """本轮下发给模型的工具面。

        分两组、各自受控:知识库那三个由 ``use_rag`` 决定(界面上的「知识库」开关),
        workspace 那几个由自己的开关决定。二者解耦是必要的——查天气或算数不需要
        知识库,把它们绑在同一个开关上,用户关掉知识库就连计算器都没了。

        workspace 工具默认全部关闭,所以默认行为与只有知识库工具时逐位相同。
        """
        tools: list[ToolDefinition] = []
        if use_rag:
            tools.extend(self._create_knowledge_tools(db, user_id, citation_sink))
        tools.extend(
            workspace_tools.build(db, user_id, self._get_knowledge_service())
        )
        return tools

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

    def _create_knowledge_tools(
        self,
        db: Session,
        user_id: str,
        citation_sink: list[dict] | None = None,
    ) -> list[ToolDefinition]:
        knowledge_service = self._get_knowledge_service()

        async def search_knowledge(arguments: dict[str, Any]) -> str:
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return "检索失败：query 必须是非空字符串。"
            context, citations = await knowledge_service.build_rag_context_with_citations(
                db,
                query.strip(),
                user_id,
                top_k=settings.RAG_TOP_K,
            )
            # 工具处理器不能直接产出 SSE 事件,命中的引用先放进 sink,由循环负责发出
            if citation_sink is not None:
                citation_sink.extend(citations)
            return context or (
                "本地知识库中未找到达到相关度要求的参考内容。"
                "可以换一种说法重新检索，或调用 list_knowledge_documents 查看有哪些文档。"
            )

        async def list_knowledge_documents(_arguments: dict[str, Any]) -> str:
            documents = await knowledge_service.get_documents(db, user_id)
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
                db, user_id, document_id.strip(), chunk_index
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

    async def _prefetch_rag_context(
        self, db: Session, user_id: str, prompt: str
    ) -> tuple[str, list[dict], bool]:
        """首轮之前的一次性检索。失败时返回空内容并标记 failed,不影响主流程。"""
        try:
            context, citations = (
                await self._get_knowledge_service().build_rag_context_with_citations(
                    db, prompt, user_id, top_k=settings.RAG_TOP_K
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
        async with tracer.trace(
            user_id=user_id, chat_id=chat_id, message_id=message_id
        ) as trace:
            async with tracer.span(
                "chat.turn",
                SpanKind.AGENT,
                model=model or settings.LLM_MODEL,
                use_rag=use_rag,
                prefetch=settings.RAG_PREFETCH if use_rag else None,
                # 只记版本号,不记正文:span 属性里永不出现提示词内容
                prompt_version=system_template.ref,
            ) as turn:
                resolved_model = model or settings.LLM_MODEL
                hit = await semantic_cache.lookup(
                    user_id,
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
                        user_id,
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
        chat_id: str,
        message_id: str | None,
        round_index: int,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """把子代理内部轨迹翻译成主 SSE 流，并逐步落库。

        子代理有自己的轮次，但把那个数字直接塞进 ``round`` 会和主代理轮次冲突：
        UI 会把 researcher 的第 1 轮排到主代理第 1 轮旁边，看起来像并行调用。
        所以 SSE 的 ``round`` 保持外层主代理轮次，内层轮次单独放 ``agentRound``。
        数据库仍用外层轮次排序，``agent_role`` 用来区分归属。
        """
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
                chat_id=chat_id,
                message_id=message_id,
                round_index=round_index,
                call_index=10_000 + index,
                tool_name=step.tool,
                status=step.status,
                result=step.result,
                tool_call_id=step.tool_call_id,
                arguments=step.arguments,
                citations=step.citations,
                agent_role=outcome.role,
            )
        yield {
            "type": "agent_state",
            "agent": outcome.role,
            "status": "failed" if outcome.failed else "completed",
            "round": round_index,
            "rounds": outcome.rounds,
            "steps": len(outcome.steps),
            "truncated": outcome.truncated,
        }

    async def _run_turn(
        self,
        db: Session,
        user_id: str,
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
        工具结果预算耗尽、或本轮请求的工具全部不可用。
        """
        citations: list[dict] = []
        budget = _ToolResultBudget(
            settings.TOOL_RESULT_TOTAL_CHARS, settings.TOOL_RESULT_MAX_CHARS
        )
        generation: dict[str, Any] = {
            "model": model or settings.LLM_MODEL,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }

        base_tools = self._create_tools(db, user_id, use_rag, citations)
        # ---- 多代理:委派 ----
        # 子代理的运行时拿的是**未经角色过滤的**完整工具集合,按角色过滤发生在
        # SubAgentRunner._schemas_for(下发哪些 schema)和它的执行前检查(越权拦截)
        # 两处。让运行时本身只装该角色的工具也能做到,但那样每次委派都要重建一次
        # ToolRuntime,而这几个工具里有的持有数据库会话与 HTTP 客户端。
        delegations: _Delegations | None = None
        tools = base_tools
        if subagent.enabled():
            registered = {tool.name for tool in base_tools}
            roles = agent_roles.available(registered)
            if roles:
                delegations = _Delegations(settings.AGENT_MAX_DELEGATIONS)
                runner = subagent.SubAgentRunner(
                    self.model_adapter,
                    ToolRuntime(base_tools),
                    generation=generation,
                    take_budget=budget.take,
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

        runtime = ToolRuntime(tools)
        history = await self._get_chat_history_messages(
            db, chat_id, exclude_message_id=message_id
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
        # 条件是 use_rag 而不是"有没有工具":workspace 工具打开之后,关掉知识库的
        # 请求也会有非空的 tools,拿它当代理会让预检索在 RAG 关闭时照样触发。
        if use_rag and settings.RAG_PREFETCH:
            yield {
                "type": "tool_start",
                "tool": "search_knowledge_base",
                "input": {"query": prompt},
                "round": 0,
            }
            # 护栏埋在检索链路深处,用一个作用域收集器把命中情况带回这里
            with guardrails.collecting() as reports:
                prefetch_context, prefetch_citations, prefetch_failed = (
                    await self._prefetch_rag_context(db, user_id, prompt)
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
                arguments={"query": prompt},
                citations=prefetch_citations,
            )
            if prefetch_context:
                prefetched = True
                user_content = (
                    "[系统已预先从本地知识库检索到以下参考内容]\n"
                    + prefetch_context
                    + "\n\n[用户问题]\n"
                    + prompt
                )
                if prefetch_citations:
                    yield {"type": "citations", "items": prefetch_citations}

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._system_prompt(system_template, prefetched),
            },
            *context.messages,
            # 轨迹紧贴当前问题:它讲的是"刚刚做过什么",离问题越近越不容易被
            # 当成更早的对话内容
            *trajectory,
            {"role": "user", "content": self._user_content(user_content, turn, model)},
        ]
        max_rounds = max(1, settings.AGENT_MAX_TOOL_ROUNDS)
        emitted_any = False
        force_final = False
        round_index = 0

        while True:
            round_index += 1
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
                    return
                completion = await self._complete_fallback(
                    messages, tools_for_round, generation
                )
                if completion is None:
                    yield {"type": "error", "error": "模型调用失败，请稍后重试。"}
                    return

            if completion.protocol_error:
                yield {"type": "error", "error": completion.protocol_error}
                return

            pending_calls = [] if is_final_round else completion.tool_calls
            if not pending_calls:
                # 适配器已在流式阶段透出前 streamed_length 个字符,只补发剩余部分。
                remainder = completion.content[completion.streamed_length :]
                if remainder.strip():
                    yield {"type": "message_delta", "content": remainder}
                    return
                if emitted_any:
                    return
                # 不把空输出当成一条成功的 assistant 消息,给用户可见的错误提示。
                yield {"type": "error", "error": "模型未返回最终回答，请稍后重试。"}
                return

            # ---- 执行本轮工具调用,把结果回灌 messages 后进入下一轮 ----
            messages.append(completion.as_assistant_message())
            text_protocol_results: list[str] = []
            unavailable_count = 0

            for call_index, call in enumerate(pending_calls):
                try:
                    arguments = json.loads(call.arguments)
                except json.JSONDecodeError:
                    arguments = {}
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
                    with guardrails.collecting() as reports:
                        result = await runtime.execute(call)
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
                            chat_id=chat_id,
                            message_id=message_id,
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
                if result.status is ToolStatus.UNAVAILABLE:
                    unavailable_count += 1

                # 存的是预算裁剪之前的原文。预算约束的是"这一回合往上下文塞多少",
                # 不该顺手决定"以后还能回看多少"。
                tool_history.record(
                    db,
                    chat_id=chat_id,
                    message_id=message_id,
                    round_index=round_index,
                    call_index=call_index,
                    tool_name=call.name,
                    status=result.status.value,
                    result=result.content,
                    tool_call_id=call.id,
                    arguments=arguments,
                    citations=step_citations,
                )

                content = budget.take(result.content)
                if completion.uses_text_tool_protocol:
                    text_protocol_results.append(f"工具 {call.name} 的结果：\n{content}")
                else:
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": content}
                    )

            if completion.uses_text_tool_protocol:
                messages.append(
                    {
                        "role": "user",
                        "content": "以下是已执行工具的内部结果。请据此继续，"
                        "必要时可再次调用工具；不要展示工具调用标记或工具协议。\n\n"
                        + "\n\n".join(text_protocol_results),
                    }
                )

            # 本轮所有工具都不可用时,继续循环只会重复失败;结果预算耗尽同理。
            if unavailable_count == len(pending_calls) or budget.exhausted:
                force_final = True

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
