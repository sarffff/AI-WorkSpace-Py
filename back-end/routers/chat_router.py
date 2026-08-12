import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from auth import get_current_user
from database import get_db
from models import Message, User
from services.chat_service import ChatService
from services.settings_service import is_model_allowed, load_preferences

router = APIRouter(prefix="/chats", tags=["对话"])
chat_service = ChatService()
logger = logging.getLogger("chat_router")


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str | None = None
    chat_id: str | None = None
    use_rag: bool = False
    message_id: uuid.UUID | None = None


class CreateChatRequest(BaseModel):
    title: str = "New Chat"


class RenameChatRequest(BaseModel):
    title: str


class ReviseMessageRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=100_000)


def _assistant_message_id(user_message_id: str) -> str:
    return str(uuid.uuid5(uuid.UUID(user_message_id), "assistant-response"))


def _generation_options(user_id: str, requested_model: str | None) -> dict:
    preferences = load_preferences(user_id)
    model = requested_model or preferences["defaultModel"]
    if not is_model_allowed(model):
        raise HTTPException(status_code=400, detail="当前模型服务不支持该模型")
    return {
        "model": model,
        "temperature": float(preferences["temperature"]),
        "max_tokens": int(preferences["maxTokens"]),
        "top_p": float(preferences["topP"]),
    }


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


@router.patch("/{chat_id}")
async def rename_chat(
    chat_id: str,
    request: RenameChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """重命名对话"""
    chat = await chat_service.get_chat_by_id(db, chat_id)
    if not chat or chat.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")

    updated = await chat_service.rename_chat(db, chat_id, request.title)
    return {"id": updated.id, "title": updated.title}


@router.delete("/{chat_id}")
async def delete_chat(
    chat_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """删除对话（关联消息级联删除）"""
    chat = await chat_service.get_chat_by_id(db, chat_id)
    if not chat or chat.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")

    deleted = await chat_service.delete_chat(db, chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="对话不存在")
    return {"success": True}


@router.post("/{chat_id}/messages/{message_id}/revise")
async def revise_message(
    chat_id: str,
    message_id: str,
    request: ReviseMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除用户消息后的冗余分支,并可选地编辑内容。"""
    chat = await chat_service.get_chat_by_id(db, chat_id)
    if not chat or chat.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")
    revised = await chat_service.revise_user_message(
        db, chat_id, message_id, request.content
    )
    if not revised:
        raise HTTPException(status_code=404, detail="用户消息不存在")
    return {"success": True, "message_id": revised.id, "content": revised.content}


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

    options = _generation_options(current_user.id, request.model)
    user_message_id = str(request.message_id or uuid.uuid4())
    try:
        await chat_service.save_message(
            db,
            chat_id,
            "user",
            request.prompt,
            options["model"],
            user_message_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    assistant_message_id = _assistant_message_id(user_message_id)
    existing_assistant = (
        db.query(Message)
        .filter(Message.id == assistant_message_id, Message.chat_id == chat_id)
        .first()
    )
    if existing_assistant:
        return {
            "success": True,
            "data": existing_assistant.content,
            "chat_id": chat_id,
            "message_id": existing_assistant.id,
        }

    # 生成 AI 响应（Agent模式，内含多轮历史和 RAG 工具）
    result = await chat_service.generate_ai_response(
        db,
        current_user.id,
        chat_id,
        request.prompt,
        options["model"],
        request.use_rag,
        user_message_id,
        options["temperature"],
        options["max_tokens"],
        options["top_p"],
    )

    # 保存 AI 响应
    await chat_service.save_message(
        db,
        chat_id,
        "assistant",
        result,
        options["model"],
        assistant_message_id,
    )

    return {
        "success": True,
        "data": result,
        "chat_id": chat_id,
        "message_id": assistant_message_id,
    }


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

    options = _generation_options(current_user.id, request.model)
    user_message_id = str(request.message_id or uuid.uuid4())
    try:
        await chat_service.save_message(
            db,
            chat_id,
            "user",
            request.prompt,
            options["model"],
            user_message_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    assistant_message_id = _assistant_message_id(user_message_id)
    existing_assistant = (
        db.query(Message)
        .filter(Message.id == assistant_message_id, Message.chat_id == chat_id)
        .first()
    )

    async def event_generator():
        full_response = ""
        failed = False
        try:
            if existing_assistant:
                yield {
                    "data": json.dumps(
                        {
                            "type": "message_delta",
                            "content": existing_assistant.content,
                            "chat_id": chat_id,
                        },
                        ensure_ascii=False,
                    )
                }
                yield {
                    "data": json.dumps(
                        {
                            "type": "done",
                            "done": True,
                            "chat_id": chat_id,
                            "message_id": existing_assistant.id,
                        },
                        ensure_ascii=False,
                    )
                }
                return
            async for event in chat_service.stream_ai_response(
                db,
                current_user.id,
                chat_id,
                request.prompt,
                options["model"],
                request.use_rag,
                user_message_id,
                options["temperature"],
                options["max_tokens"],
                options["top_p"],
            ):
                payload = {**event, "chat_id": chat_id}
                if event["type"] == "message_delta":
                    full_response += event["content"]
                if event["type"] == "error":
                    failed = True
                yield {"data": json.dumps(payload, ensure_ascii=False)}
                if failed:
                    break

            # 仅持久化最终面向用户的文本;工具事件为瞬时状态,不落库。
            if not failed and full_response.strip():
                await chat_service.save_message(
                    db,
                    chat_id,
                    "assistant",
                    full_response,
                    options["model"],
                    assistant_message_id,
                )
                yield {
                    "data": json.dumps(
                        {
                            "type": "done",
                            "done": True,
                            "chat_id": chat_id,
                            "message_id": assistant_message_id,
                        },
                        ensure_ascii=False,
                    )
                }
            elif not failed:
                # 防御性兜底:永远不要将空模型输出标记为成功的 assistant 消息,
                # 即便未来某个 service 实现忘了发出 error 事件。
                yield {
                    "data": json.dumps(
                        {"type": "error", "error": "模型未返回最终回答，请稍后重试。"},
                        ensure_ascii=False,
                    )
                }
        except Exception:
            logger.exception("Chat stream failed for chat %s", chat_id)
            yield {
                "data": json.dumps(
                    {"type": "error", "error": "生成回答失败，请稍后重试。"},
                    ensure_ascii=False,
                )
            }

    return EventSourceResponse(event_generator())
