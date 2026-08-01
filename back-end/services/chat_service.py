from typing import AsyncGenerator
from sqlalchemy.orm import Session
from openai import AsyncOpenAI
from config import settings
from models import Chat, Message


class ChatService:
    def __init__(self):
        self.openai = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )

    async def get_recent_chats(self, db: Session, user_id: str) -> list[dict]:
        """获取最近的聊天记录（从数据库）"""
        chats = (
            db.query(Chat)
            .filter(Chat.user_id == user_id)
            .order_by(Chat.updated_at.desc())
            .limit(20)
            .all()
        )
        return [
            {
                "id": chat.id,
                "title": chat.title,
                "createdAt": chat.created_at.isoformat(),
                "updatedAt": chat.updated_at.isoformat(),
            }
            for chat in chats
        ]

    async def get_chat_by_id(self, db: Session, chat_id: str) -> Chat | None:
        """根据ID获取对话"""
        return db.query(Chat).filter(Chat.id == chat_id).first()

    async def get_chat_messages(self, db: Session, chat_id: str) -> list[dict]:
        """获取指定对话的消息列表"""
        messages = (
            db.query(Message)
            .filter(Message.chat_id == chat_id)
            .order_by(Message.created_at.asc())
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

    async def create_chat(self, db: Session, user_id: str, title: str = "New Chat") -> Chat:
        """创建新对话"""
        chat = Chat(user_id=user_id, title=title)
        db.add(chat)
        db.commit()
        db.refresh(chat)
        return chat

    async def save_message(self, db: Session, chat_id: str, role: str, content: str, model: str | None = None) -> Message:
        """保存消息到数据库"""
        message = Message(chat_id=chat_id, role=role, content=content, model=model)
        db.add(message)
        db.commit()
        db.refresh(message)
        return message

    async def _get_chat_history(self, db: Session, chat_id: str, limit: int = 20) -> list[dict]:
        """获取对话历史，格式化为 OpenAI API 消息列表"""
        messages = (
            db.query(Message)
            .filter(Message.chat_id == chat_id)
            .order_by(Message.created_at.asc())
            .limit(limit)
            .all()
        )
        return [{"role": msg.role, "content": msg.content} for msg in messages]

    async def generate_ai_response(
        self, db: Session, chat_id: str, model: str | None = None, rag_context: str = ""
    ) -> str:
        """生成 AI 响应（非流式，带历史上下文和可选的 RAG 上下文）"""
        history = await self._get_chat_history(db, chat_id)
        messages = self._build_messages(history, rag_context)
        completion = await self.openai.chat.completions.create(
            model=model or settings.LLM_MODEL,
            messages=messages,
        )
        return completion.choices[0].message.content or ""

    async def stream_ai_response(
        self, db: Session, chat_id: str, model: str | None = None, rag_context: str = ""
    ) -> AsyncGenerator[str, None]:
        """流式生成 AI 响应（带历史上下文和可选的 RAG 上下文）"""
        history = await self._get_chat_history(db, chat_id)
        messages = self._build_messages(history, rag_context)
        stream = await self.openai.chat.completions.create(
            model=model or settings.LLM_MODEL,
            messages=messages,
            stream=True,
        )
        async for chunk in stream:
            content = chunk.choices[0].delta.content
            if content:
                yield content

    @staticmethod
    def _build_messages(history: list[dict], rag_context: str) -> list[dict]:
        """构建消息列表，如果提供了 RAG 上下文则作为系统消息注入"""
        if rag_context:
            system_msg = {
                "role": "system",
                "content": (
                    "你是一个知识库助手。请根据以下参考内容回答用户的问题。"
                    "如果参考内容不足以回答问题，请如实说明。\n\n" + rag_context
                ),
            }
            return [system_msg] + history
        return history
