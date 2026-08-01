from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from auth import get_current_user
from database import get_db
from models import User
from services.chat_service import ChatService
from services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/chats", tags=["对话"])
chat_service = ChatService()
knowledge_service = KnowledgeService()


class ChatRequest(BaseModel):
    prompt: str
    model: str | None = None
    chat_id: str | None = None
    use_rag: bool = False


class CreateChatRequest(BaseModel):
    title: str = "New Chat"


async def _get_rag_context(db: Session, prompt: str) -> str:
    """获取 RAG 知识库上下文"""
    try:
        return await knowledge_service.build_rag_context(db, prompt)
    except Exception:
        return ""


@router.get("")
async def get_chats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取当前用户的对话列表"""
    return await chat_service.get_recent_chats(db, user_id=current_user.id)


@router.get("/{chat_id}/messages")
async def get_chat_messages(
    chat_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """获取指定对话的消息列表"""
    # 验证对话所有权
    chat = await chat_service.get_chat_by_id(db, chat_id)
    if not chat or chat.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")
    
    return await chat_service.get_chat_messages(db, chat_id)


@router.post("")
async def create_chat(
    request: CreateChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """创建新对话"""
    chat = await chat_service.create_chat(db, title=request.title, user_id=current_user.id)
    return {"id": chat.id, "title": chat.title}


@router.post("/completions")
async def completions(
    request: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """非流式对话"""
    # 如果没有 chat_id，创建新对话
    chat_id = request.chat_id
    if not chat_id:
        chat = await chat_service.create_chat(db, title=request.prompt[:50], user_id=current_user.id)
        chat_id = chat.id
    else:
        # 验证对话所有权
        chat = await chat_service.get_chat_by_id(db, chat_id)
        if not chat or chat.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="对话不存在")

    # 保存用户消息
    await chat_service.save_message(db, chat_id, "user", request.prompt, request.model)

    # RAG 检索
    rag_context = await _get_rag_context(db, request.prompt) if request.use_rag else ""

    # 生成 AI 响应（带历史上下文 + RAG）
    result = await chat_service.generate_ai_response(db, chat_id, request.model, rag_context)

    # 保存 AI 响应
    await chat_service.save_message(db, chat_id, "assistant", result, request.model)

    return {"success": True, "data": result, "chat_id": chat_id}


@router.post("/completions/stream")
async def stream_completions(
    request: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """流式对话 SSE"""
    # 如果没有 chat_id，创建新对话
    chat_id = request.chat_id
    if not chat_id:
        chat = await chat_service.create_chat(db, title=request.prompt[:50], user_id=current_user.id)
        chat_id = chat.id
    else:
        # 验证对话所有权
        chat = await chat_service.get_chat_by_id(db, chat_id)
        if not chat or chat.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="对话不存在")

    # 保存用户消息
    await chat_service.save_message(db, chat_id, "user", request.prompt, request.model)

    # RAG 检索
    rag_context = await _get_rag_context(db, request.prompt) if request.use_rag else ""

    async def event_generator():
        full_response = ""
        try:
            async for chunk in chat_service.stream_ai_response(
                db, chat_id, request.model, rag_context
            ):
                full_response += chunk
                yield {"data": f'{{"content": "{chunk}", "chat_id": "{chat_id}"}}'}

            # 流结束后保存 AI 响应
            await chat_service.save_message(db, chat_id, "assistant", full_response, request.model)
            yield {"data": '{"done": true}'}
        except Exception as e:
            yield {"data": f'{{"error": "{str(e)}"}}'}

    return EventSourceResponse(event_generator())