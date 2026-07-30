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

    async def get_recent_chats(self, db: Session, user_id: str = "default") -> list[dict]:
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
                "date": chat.updated_at.strftime("%Y-%m-%d %H:%M"),
            }
            for chat in chats
        ]

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
                "timestamp": msg.created_at.strftime("%H:%M"),
            }
            for msg in messages
        ]

    async def create_chat(self, db: Session, user_id: str = "default", title: str = "New Chat") -> Chat:
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

    async def generate_ai_response(self, prompt: str, model: str | None = None) -> str:
        """生成 AI 响应（非流式，对应 NestJS 的 generateAiResponse）"""
        completion = await self.openai.chat.completions.create(
            model=model or settings.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        return completion.choices[0].message.content or ""

    async def stream_ai_response(
        self, prompt: str, model: str | None = None
    ) -> AsyncGenerator[str, None]:
        """流式生成 AI 响应（对应 NestJS 的 streamAiResponse）"""
        stream = await self.openai.chat.completions.create(
            model=model or settings.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        async for chunk in stream:
            content = chunk.choices[0].delta.content
            if content:
                yield content
