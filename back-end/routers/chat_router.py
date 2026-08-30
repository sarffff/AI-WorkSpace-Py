import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from auth import get_current_user
from config import settings
from database import get_db, SessionLocal
from models import Message, User
from services.chat_service import ChatService
from services.memory_service import memory_service
from services import approval
from services import checkpoint_store
from services import approval_audit
from services import prompt_library
from services import usage_guard
from services.settings_service import is_model_allowed, load_preferences

router = APIRouter(prefix="/chats", tags=["对话"])
chat_service = ChatService()
logger = logging.getLogger("chat_router")


def _enforce_usage(db: Session, user_id: str) -> None:
    """用量闸门。撞上上限抛 429。

    **必须在进入 SSE 之前调用**,理由和 ``resume_run`` 里那句归属校验一样:
    生成器里抛 HTTPException 只会得到一条已经建立、然后突然断掉的流,
    前端拿不到状态码,用户看到的是"连上了又没了",而不是"你被限流了"。

    ``Retry-After`` 是标准头,客户端与反向代理都认。只有频率那道闸门给得出
    可信的等待时间——成本与 token 要等窗口滑过去,而窗口是 24 小时量级,
    给一个精确到秒的数字没有意义。
    """
    rejection = usage_guard.check(db, user_id)
    if rejection is None:
        return
    logger.info("usage guard rejected user=%s kind=%s", user_id, rejection.kind)
    headers = (
        {"Retry-After": str(rejection.retry_after)}
        if rejection.retry_after is not None
        else None
    )
    raise HTTPException(status_code=429, detail=rejection.message, headers=headers)


def _reject_if_expired(db: Session, run) -> None:
    """审批超时的执行不允许恢复，就地标成 abandoned 并抛 409。

    这道判断**不依赖过期扫描跑过**。扫描挂在 ``/runs/pending`` 这个读路径上，
    也就是说"一直没人打开审批列表"时它一次都不会发生——而恢复恰恰是那种情况下
    真正要拦住的地方：一个三天前的审批现在被点同意，工具面会按**当下**的权限
    重建，当初批准的语境早就不存在了。
    """
    if not approval_audit.is_expired(run.updated_at):
        return
    hours = settings.AGENT_APPROVAL_TIMEOUT_HOURS
    checkpoint_store.expire_stale_runs(db, run.user_id)
    logger.info("rejected resume of expired run %s", run.id)
    raise HTTPException(
        status_code=409,
        detail=(
            f"这次审批已超过 {hours} 小时时限，不能再恢复。"
            "请重新提问——工具权限与知识库内容可能都已经变了。"
        ),
    )


async def _extract_memory(user_id: str, chat_id: str, question: str, answer: str) -> None:
    """长期记忆抽取:SSE 流已结束,这里自建会话后台跑,绝不拖慢对话。

    请求作用域的 db 会话在响应结束后就关了,所以必须另开一个;失败只记
    日志——记忆是增强,抽取挂掉不该让用户看到任何错误。
    """
    try:
        with SessionLocal() as db:
            await memory_service.extract(
                chat_service.model_adapter,
                db,
                user_id=user_id,
                chat_id=chat_id,
                question=question,
                answer=answer,
            )
    except Exception:
        logger.warning("memory extraction failed", exc_info=True)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str | None = None
    chat_id: str | None = None
    use_rag: bool = False
    message_id: uuid.UUID | None = None
    # 只对这一次请求生效的系统提示词版本。不传则用 settings 里的默认版本。
    # 做成请求级而不是全局开关:提示词实验台上试新版时,别人的对话不该被改。
    prompt_version: str | None = None


class CreateChatRequest(BaseModel):
    title: str = "New Chat"


class RenameChatRequest(BaseModel):
    title: str


class ReviseMessageRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=100_000)


class ResumeRequest(BaseModel):
    """审批裁决。

    ``approved`` 没有默认值:审批这件事不该有默认。漏传字段就该 422,
    而不是按"同意"或"拒绝"里的任何一个静默处理。
    """

    approved: bool
    # 拒绝时的补充说明。会随拒绝结果回灌给模型——那通常正好是它需要的修改方向
    note: str = Field(default="", max_length=2000)
    # 改过参数再放行。``None`` = 没编辑，原样执行；给了就必须同时 approved=True
    # （校验见下面的 validator）。
    #
    # 为什么不做成独立的 verdict 枚举："改完执行"在语义上就是一种批准——它经过
    # 同一道闸门、同一套 schema 校验、写同一批 approved_call_ids。做成第三种裁决
    # 会让下游每个判断 ``if approved`` 的地方都得再想一次"编辑算不算同意"。
    editedArguments: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _edit_requires_approval(self) -> "ResumeRequest":
        """拒绝时不接受参数修改。

        ``approved=False`` 配上 ``editedArguments`` 是个自相矛盾的请求：不执行的
        调用没有参数可言。静默忽略那个字段会让客户端的 bug 变成"我改了但没生效"，
        所以这里直接 422。
        """
        if self.editedArguments is not None and not self.approved:
            raise ValueError("拒绝时不能同时修改参数")
        return self


def _assistant_message_id(user_message_id: str) -> str:
    return str(uuid.uuid5(uuid.UUID(user_message_id), "assistant-response"))


def _prompt_version(requested: str | None) -> str | None:
    """校验请求指定的系统提示词版本。

    版本不存在直接 400,不做"回退到默认版本"——静默回退会让实验组的请求
    悄悄跑成对照组,拿到的对比结论是错的,而且没有任何迹象。
    """
    if requested is None:
        return None
    available = prompt_library.available_request_versions()
    if requested not in available:
        raise HTTPException(
            status_code=400, detail=f"提示词版本不存在，可用：{available}"
        )
    return requested


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


@router.get("/{chat_id}/tool-steps")
async def get_chat_tool_steps(
    chat_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取指定对话的工具执行轨迹（只读）。

    和消息分成两个接口:SSE 里的 tool_start / tool_result 是瞬时事件,刷新页面
    就没了,这里是把那条时间线找回来的唯一入口。
    """
    chat = await chat_service.get_chat_by_id(db, chat_id)
    if not chat or chat.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")

    return {"steps": await chat_service.get_chat_tool_steps(db, chat_id)}


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
    _enforce_usage(db, current_user.id)
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
        _prompt_version(request.prompt_version),
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
    _enforce_usage(db, current_user.id)
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
    # 在进入 SSE 生成器之前校验:生成器里抛 HTTPException 只会得到一条
    # 已经建立、然后突然断掉的流,前端拿不到 400 也看不到原因。
    prompt_version = _prompt_version(request.prompt_version)
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
        # 被审批打断了。这一回合还没有最终回答，所以既不落 assistant 消息，
        # 也不能报"模型未返回最终回答"——它没失败，它在等人。
        interrupted = False
        # 这一回合的 run id 与"有没有走到自然结束"。两者一起决定断线时怎么收尾。
        #
        # run_id 只能从事件流里拿:它是在 chat_service 内部生成的,路由这边
        # 事先不知道。``run_started`` 正是为此在任何东西可能断掉之前就发出。
        run_id: str | None = None
        settled = False
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
                prompt_version,
            ):
                payload = {**event, "chat_id": chat_id}
                if event["type"] == "run_started":
                    run_id = event.get("runId")
                if event["type"] == "message_delta":
                    full_response += event["content"]
                if event["type"] == "error":
                    failed = True
                if event["type"] == "approval_required":
                    interrupted = True
                    # 带上 message_id：恢复那一侧要用同一个 id 落最终回答，
                    # 否则一次问答会在库里留下两条 assistant 消息。
                    payload["message_id"] = assistant_message_id
                yield {"data": json.dumps(payload, ensure_ascii=False)}
                if failed:
                    break

            # 这里只落最终面向用户的文本。工具轨迹已经在循环内逐步写进
            # message_tool_steps 了,不需要(也不该)等到流跑完才存——
            # 流被掐断时那几步的检索成本是真花掉了。
            if interrupted:
                # 状态在 agent_checkpoints 里，恢复走 /runs/{run_id}/resume。
                # 这里什么都不落：半截回答不是回答。
                settled = True
                return
            if not failed and full_response.strip():
                await chat_service.save_message(
                    db,
                    chat_id,
                    "assistant",
                    full_response,
                    options["model"],
                    assistant_message_id,
                )
                # 回答真正落库之后才抽记忆:抽到一半流断掉的情况,记忆来源
                # (question/answer)都已经完整拿到,不会存进半截事实
                if settings.MEMORY_ENABLED:
                    asyncio.create_task(
                        _extract_memory(
                            current_user.id, chat_id, request.prompt, full_response
                        )
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
            settled = True
        except Exception:
            logger.exception("Chat stream failed for chat %s", chat_id)
            settled = True
            yield {
                "data": json.dumps(
                    {"type": "error", "error": "生成回答失败，请稍后重试。"},
                    ensure_ascii=False,
                )
            }
        finally:
            # 客户端断线时这个 finally 会在生成器被回收时执行,而上面的正常收尾
            # 一条都没走到——``settled`` 就是用来区分这两种情况的。
            #
            # 不标的话这一行会永远停在 ``running``,要等 orphan_timeout_seconds()
            # (约 780 秒)才被回收,而**静默重连等不了那么久**。标成 interrupted
            # 让它立刻可接续;进程真的崩掉、连 finally 都没跑到的情况仍由孤儿
            # 回收兜着。
            if not settled and run_id:
                checkpoint_store.mark_interrupted(run_id)

    return EventSourceResponse(event_generator())


@router.get("/runs/pending")
async def pending_runs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """当前用户所有等待审批的执行。

    刷新页面之后靠它把审批卡片找回来。这是可恢复执行与"挂一个长连接等用户点"
    最直观的区别:中断活在数据库里,不活在那条已经断掉的 SSE 连接里。
    """
    # 顺带把超时的清掉。放在读路径上而不是起一个后台调度器:项目里没有 worker
    # 进程,而这个列表本来就要查这批数据。副作用是"一直没人打开审批列表"时清理
    # 不会发生——所以恢复那边另有一道独立的时限判断,不依赖这里跑过。
    checkpoint_store.expire_stale_runs(db, current_user.id)
    approval_audit.expire_stale(db)
    runs = checkpoint_store.list_pending(db, current_user.id)
    items = []
    for run in runs:
        state = checkpoint_store.latest(db, run.id)
        request = state.interrupt_request if state else None
        items.append(
            {
                "runId": run.id,
                "chatId": run.chat_id,
                "messageId": run.message_id,
                "round": state.round_index if state else run.rounds,
                "interrupts": run.interrupts,
                "updatedAt": run.updated_at.isoformat() if run.updated_at else None,
                "tool": request.tool if request else None,
                "reason": request.reason if request else "",
                "preview": (
                    approval.build_preview(request.arguments) if request else {}
                ),
            }
        )
    return items


def _continuation_sse(
    stream,
    *,
    db: Session,
    user_id: str,
    run_id: str,
    chat_id: str,
    assistant_message_id: str | None,
    prefix: str,
    model: str | None,
    state_before,
    what: str,
):
    """把一条"接着跑"的事件流包成 SSE。

    审批恢复和澄清恢复共用它。这段逻辑里有三处不显然的东西，各写两遍必然会分叉：

    1. **再次中断时不落库。** 一次恢复可以再停一次（第二个写操作要审批，或者
       模型拿到答案后又问一个问题）。那时 ``full_response`` 只是半截回答，
       落库会让用户在历史里看到一句没写完的话。
    2. **落库要接上 ``streamed_prefix``。** 中断之前可能已经有正文流给用户了
       （"我来把这份整理好保存进知识库"）。不接的话数据库里的回答比用户看到的少一句。
    3. **assistant id 是 uuid5(user_message_id) 算出来的。** 与被打断那次请求
       一致，所以一问一答不会留下两条 assistant 消息。
    """

    async def event_generator():
        full_response = ""
        failed = False
        interrupted = False
        try:
            async for event in stream:
                payload = {**event, "chat_id": chat_id}
                if event["type"] == "message_delta":
                    full_response += event["content"]
                if event["type"] == "error":
                    failed = True
                if event["type"] == "approval_required" or (
                    event["type"] == "clarification" and event.get("resumable")
                ):
                    interrupted = True
                    payload["message_id"] = assistant_message_id
                yield {"data": json.dumps(payload, ensure_ascii=False)}
                if failed:
                    break

            if interrupted:
                return
            answer = prefix + full_response
            if not failed and answer.strip() and assistant_message_id:
                await chat_service.save_message(
                    db, chat_id, "assistant", answer, model, assistant_message_id
                )
                if settings.MEMORY_ENABLED and state_before is not None:
                    asyncio.create_task(
                        _extract_memory(user_id, chat_id, state_before.prompt, answer)
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
                yield {
                    "data": json.dumps(
                        {"type": "error", "error": f"{what}后模型未返回最终回答。"},
                        ensure_ascii=False,
                    )
                }
        except Exception:
            logger.exception("%s failed for run %s", what, run_id)
            yield {
                "data": json.dumps(
                    {"type": "error", "error": f"{what}失败，请稍后重试。"},
                    ensure_ascii=False,
                )
            }

    return EventSourceResponse(event_generator())


class ClarificationAnswerRequest(BaseModel):
    """对 ``ask_user`` 那个问题的回答。

    没有默认值，空串由服务层挡下并保留 ``waiting_input``——用户可以再答一次。
    """

    answer: str = Field(min_length=1, max_length=10_000)


@router.post("/runs/{run_id}/answer")
async def answer_clarification(
    run_id: str,
    request: ClarificationAnswerRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """回答模型的澄清问题，并**接着那一轮**跑下去。

    与 ``/resume`` 是两个端点而不是一个带 mode 的端点：它们的载荷没有交集
    （一个是裁决 + 可选的参数修改，一个是一句话），前置状态校验也不同
    （``waiting_approval`` vs ``waiting_input``）。合成一个的话每个字段都得
    写"仅当 mode=X 时有效"。
    """
    run = checkpoint_store.get_run(db, run_id)
    if run is None or run.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    if run.status != "waiting_input":
        raise HTTPException(
            status_code=409, detail=f"该执行当前状态为 {run.status}，不在等待回答"
        )
    _reject_if_expired(db, run)
    # 恢复也要过闸门:它接着跑的是同一个 Agent 循环,剩下几轮照样花钱。
    # 代价是配额耗尽时这个 run 会卡在 waiting_input 里,得等窗口滑过去——
    # 但那正是成本上限的语义。归属校验放在前面,所以 404 优先于 429:
    # 不该让别人的 run 存不存在这件事被限流状态泄露出去。
    _enforce_usage(db, current_user.id)

    state_before = checkpoint_store.latest(db, run_id)
    return _continuation_sse(
        chat_service.answer_clarification(
            db, current_user.id, run_id, answer=request.answer
        ),
        db=db,
        user_id=current_user.id,
        run_id=run_id,
        chat_id=run.chat_id,
        assistant_message_id=(
            _assistant_message_id(run.message_id) if run.message_id else None
        ),
        prefix=state_before.streamed_prefix if state_before else "",
        model=run.model,
        state_before=state_before,
        what="回答",
    )


@router.get("/runs/resumable")
async def resumable_runs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """断线留下、可以接着跑的执行。

    与 ``/runs/pending`` 分开:那个列的是"等你做决定"(``waiting_*``),这个列的是
    "没人做错什么,连接断了"。界面上是两种不同的提示——前者要人裁决,后者只要
    问一句"接着跑吗"。

    顺带把真正回收不了的孤儿标掉(超时且没有安全接续点的)。
    """
    checkpoint_store.reap_orphan_runs(db, current_user.id)
    items = []
    for run in checkpoint_store.list_resumable(db, current_user.id):
        state = checkpoint_store.latest(db, run.id)
        # 只有停在 post_tools 的才接得上(见 continue_orphan 的文档串)。
        # 接不上的不列出来——给用户一个点了只会报错的按钮比不给更糟。
        if state is None or state.phase != "post_tools":
            continue
        items.append(
            {
                "runId": run.id,
                "chatId": run.chat_id,
                "messageId": run.message_id,
                "round": state.round_index,
                "updatedAt": run.updated_at.isoformat() if run.updated_at else None,
            }
        )
    return {"items": items}


@router.post("/runs/{run_id}/continue")
async def continue_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """接上一个因断线而没跑完的回合。

    与 ``/resume`` 是两个端点:那个要带裁决(同意/拒绝/改参数),这个没有任何载荷
    ——没有人做错什么,是连接断了。合成一个会让 ``approved`` 字段在断线场景下
    变成一个没有意义但必填的值。
    """
    run = checkpoint_store.get_run(db, run_id)
    if run is None or run.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    if run.status not in ("interrupted", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"该执行当前状态为 {run.status}，不是断线未完成",
        )
    # 接着跑等于接着花钱,同 resume。
    _enforce_usage(db, current_user.id)

    state_before = checkpoint_store.latest(db, run_id)
    if state_before is None or state_before.phase != "post_tools":
        # 在进入 SSE 之前判:生成器里报错只会得到一条建立后立刻断掉的流。
        raise HTTPException(
            status_code=409,
            detail="这次执行断在工具执行中途，无法安全接续（可能造成重复写入）。请重新提问。",
        )

    return _continuation_sse(
        chat_service.continue_orphan(db, current_user.id, run_id),
        db=db,
        user_id=current_user.id,
        run_id=run_id,
        chat_id=run.chat_id,
        assistant_message_id=(
            _assistant_message_id(run.message_id) if run.message_id else None
        ),
        prefix=state_before.streamed_prefix,
        model=run.model,
        state_before=state_before,
        what="接续",
    )


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    request: ResumeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """裁决一次审批并接着跑完这一回合。

    响应同样是 SSE:恢复之后模型还要继续几轮工具调用、最后流式给出回答,
    和普通对话没有区别。区别只在它是从数据库里的一份快照接上的。

    最终回答落库用的是 ``uuid5(user_message_id)`` 算出来的同一个 assistant id
    ——与被打断的那次请求一致,所以一次问答不会留下两条 assistant 消息。
    """
    run = checkpoint_store.get_run(db, run_id)
    # 归属与状态在进入 SSE 之前校验:生成器里抛 HTTPException 只会得到一条
    # 已经建立、然后突然断掉的流,前端拿不到状态码也看不到原因。
    if run is None or run.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    if run.status != "waiting_approval":
        raise HTTPException(
            status_code=409, detail=f"该执行当前状态为 {run.status}，不在等待审批"
        )
    _reject_if_expired(db, run)
    # 同 answer_clarification：恢复接着跑同一个循环，剩下几轮照样花钱。
    _enforce_usage(db, current_user.id)

    chat_id = run.chat_id
    assistant_message_id = (
        _assistant_message_id(run.message_id) if run.message_id else None
    )
    state_before = checkpoint_store.latest(db, run_id)
    prefix = state_before.streamed_prefix if state_before else ""
    model = run.model

    return _continuation_sse(
        chat_service.resume_turn(
            db,
            current_user.id,
            run_id,
            approved=request.approved,
            note=request.note,
            edited_arguments=request.editedArguments,
        ),
        db=db,
        user_id=current_user.id,
        run_id=run_id,
        chat_id=chat_id,
        assistant_message_id=assistant_message_id,
        prefix=prefix,
        model=model,
        state_before=state_before,
        what="恢复",
    )


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """一次执行的详情:状态、轮次、委派数、被打断过几次、子代理、快照目录。

    快照目录（``checkpoints``）只给元信息不给正文:那里面是整段 messages,
    一个调试接口没有理由把整段对话再吐一遍。
    """
    run = checkpoint_store.get_run(db, run_id)
    if run is None or run.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    children = checkpoint_store.child_runs(db, run_id)
    return {
        "runId": run.id,
        "chatId": run.chat_id,
        "messageId": run.message_id,
        "agentRole": run.agent_role,
        "status": run.status,
        "rounds": run.rounds,
        "delegations": run.delegations,
        "interrupts": run.interrupts,
        "model": run.model,
        "promptRef": run.prompt_ref,
        "traceId": run.trace_id,
        "errorType": run.error_type,
        "startedAt": run.started_at.isoformat() if run.started_at else None,
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
        "children": [
            {
                "runId": child.id,
                "agentRole": child.agent_role,
                "status": child.status,
                "rounds": child.rounds,
                "errorType": child.error_type,
            }
            for child in children
        ],
        "checkpoints": checkpoint_store.history(db, run_id),
        # 审批留痕。挂在这里而不是单开一个端点:问"这次执行被批过什么"的人
        # 和问"这次执行走了几轮"的人是同一个人,分两个端点只是让他多发一次请求。
        #
        # 一定要有一个读路径,否则就是"写进库了但没有任何地方看得到"——审计表最
        # 常见的失效方式恰恰是这个,而它在测试里完全看不出来。
        "approvals": approval_audit.history(db, run_id),
    }
