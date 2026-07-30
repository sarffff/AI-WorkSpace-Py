from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from database import get_db
from services.chat_service import ChatService

router = APIRouter(prefix="/chats", tags=["chats"])
chat_service = ChatService()


class ChatRequest(BaseModel):
    prompt: str
    model: str | None = None
    chat_id: str | None = None


class CreateChatRequest(BaseModel):
    title: str = "New Chat"


@router.get("")
async def get_chats(db: Session = Depends(get_db)):
    """获取最近对话列表（从数据库）"""
    return await chat_service.get_recent_chats(db)


@router.get("/{chat_id}/messages")
async def get_chat_messages(chat_id: str, db: Session = Depends(get_db)):
    """获取指定对话的消息列表"""
    return await chat_service.get_chat_messages(db, chat_id)


@router.post("")
async def create_chat(request: CreateChatRequest, db: Session = Depends(get_db)):
    """创建新对话"""
    chat = await chat_service.create_chat(db, title=request.title)
    return {"id": chat.id, "title": chat.title}


@router.post("/completions")
async def completions(request: ChatRequest, db: Session = Depends(get_db)):
    """非流式对话（对应 NestJS 的 @Post('completions')）"""
    # 如果没有 chat_id，创建新对话
    chat_id = request.chat_id
    if not chat_id:
        chat = await chat_service.create_chat(db, title=request.prompt[:50])
        chat_id = chat.id

    # 保存用户消息
    await chat_service.save_message(db, chat_id, "user", request.prompt, request.model)

    # 生成 AI 响应
    result = await chat_service.generate_ai_response(request.prompt, request.model)

    # 保存 AI 响应
    await chat_service.save_message(db, chat_id, "assistant", result, request.model)

    return {"success": True, "data": result, "chat_id": chat_id}


@router.post("/completions/stream")
async def stream_completions(request: ChatRequest, db: Session = Depends(get_db)):
    """流式对话 SSE（对应 NestJS 的 @Post('completions/stream')）"""
    # 如果没有 chat_id，创建新对话
    chat_id = request.chat_id
    if not chat_id:
        chat = await chat_service.create_chat(db, title=request.prompt[:50])
        chat_id = chat.id

    # 保存用户消息
    await chat_service.save_message(db, chat_id, "user", request.prompt, request.model)

    async def event_generator():
        full_response = ""
        try:
            async for chunk in chat_service.stream_ai_response(
                request.prompt, request.model
            ):
                full_response += chunk
                yield {"data": f'{{"content": "{chunk}", "chat_id": "{chat_id}"}}'}

            # 流结束后保存 AI 响应
            await chat_service.save_message(db, chat_id, "assistant", full_response, request.model)
            yield {"data": '{"done": true}'}
        except Exception as e:
            yield {"data": f'{{"error": "{str(e)}"}}'}

    return EventSourceResponse(event_generator())
