import logging
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import SessionLocal, get_db
from models import User
from services import document_queue, egress, file_types, ingest_clean, workspace_service, workspace_tools
from services.knowledge_service import KnowledgeService
from services.workspace_service import WorkspaceError

router = APIRouter(prefix="/knowledge", tags=["知识库"])
knowledge_service = KnowledgeService()
logger = logging.getLogger("knowledge_router")

# 知识库允许的扩展名 = 能解析成文本的那些（不含图片：没有 OCR 链路）。
# 从 file_types 派生，见那里的模块文档——改动之前这是六处副本里的一处，
# 而副本之间已经不一致（.html 在前端选得到、在这里 400）。
_KNOWLEDGE_ALLOWED_EXT = file_types.KNOWLEDGE


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


def _kick_indexing(db: Session, background_tasks: BackgroundTasks, document_id: str) -> None:
    """把一篇排进持久队列并触发一次抽干。

    入队落 document_jobs 行（持久、可重试、可跨重启恢复）；抽干作为响应后的后台
    任务跑，不占 HTTP 连接。替代了改动前的 ``background_tasks.add_task(_index_document_task)``
    那条进程内 fire-and-forget——现在进程中途重启，队列里的行还在，启动重驱会接上。
    """
    if document_queue.enqueue(db, document_id) is not None:
        background_tasks.add_task(document_queue.run_pending)


@router.get("/documents")
async def get_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """文档列表：工作区共享 + 自己的个人文档；admin 另外看到成员的个人文档。

    admin 多出来的那些**不进他的检索**（见 ``HybridRetriever._retrievable_by``），
    所以每行带 ``retrievable`` 让界面把两件事分开说。他也删不掉它们
    （``require_can_modify``）——知情权和处置权是分开的。
    """
    workspace = workspace_service.resolve_for_user(db, current_user)
    # 惰性回收过期租约（认领它的进程死了的任务重新入队）——挂在读路径上,
    # 项目里没有调度器,同 expire_stale_runs 挂在 /chats/runs/pending 的做法。
    document_queue.reap_expired_leases(db)
    docs = await knowledge_service.get_documents(
        db,
        workspace.id,
        viewer_id=current_user.id,
        include_member_private=workspace_service.is_admin(current_user),
    )
    # 给还在处理/失败的文档带上队列进度与尝试次数,界面能显示"排队中/第 2 次重试"
    # 而不是一个干巴巴的 processing。只对非终态查,正常列表几乎不多花查询。
    for item in docs:
        if isinstance(item, dict) and item.get("status") in ("processing", "failed"):
            job = document_queue.job_for_document(db, item.get("id"))
            if job is not None:
                item["jobStatus"] = job.status
                item["jobProgress"] = job.progress
                item["jobAttempts"] = job.attempts
    return docs


@router.post("/documents/upload")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    visibility: str = Form("workspace"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上传文档。解析后立即返回 processing，分块与向量化在后台完成。

    ``visibility`` 默认 ``workspace``：这个端点是知识库管理页面用的，那里的用途
    就是维护团队资产。chat 附件走另一条路并显式传 ``private``——两个默认值不同，
    所以 ``resolve_upload_visibility`` 刻意不接受 None（见它的说明）。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _KNOWLEDGE_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 .{ext}，允许：{', '.join(sorted(_KNOWLEDGE_ALLOWED_EXT))}",
        )

    content = await file.read()
    max_size = 10 * 1024 * 1024  # 10MB
    if len(content) > max_size:
        raise HTTPException(status_code=400, detail=f"文件大小不能超过 {max_size // 1024 // 1024}MB")

    workspace = workspace_service.resolve_for_user(db, current_user)
    try:
        # 只有传共享文档才要 admin；个人文档谁都能传。
        # 这一句同时挡掉"user 手改请求把 visibility 填成 workspace"。
        resolved_visibility = workspace_service.resolve_upload_visibility(
            current_user, visibility
        )
    except WorkspaceError as e:
        raise HTTPException(status_code=403, detail=str(e))
    try:
        doc, duplicate = await knowledge_service.create_document(
            db,
            file.filename,
            content,
            workspace_id=workspace.id,
            uploader_id=current_user.id,
            visibility=resolved_visibility,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("文档解析失败")
        raise HTTPException(status_code=500, detail="文档解析失败，请稍后重试") from e

    # 向量化是整个流程里最慢的一环(N 次 embedding 调用),不该占着 HTTP 连接。
    # 重复上传直接返回已有文档,不再排一次索引任务。
    if not duplicate:
        _kick_indexing(db, background_tasks, doc.id)

    return {
        "id": doc.id,
        "name": doc.name,
        "size": doc.size,
        "chunks": doc.chunks,
        "status": doc.status,
        "visibility": doc.visibility,
        "duplicate": duplicate,
    }


class UrlIngestRequest(BaseModel):
    """把一个网页加入知识库。``visibility`` 与 upload 同义（默认工作区共享）。"""

    url: str = Field(min_length=1, max_length=2000)
    visibility: str = "workspace"


@router.post("/documents/from-url")
async def add_document_from_url(
    request: UrlIngestRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """把一个网页抓成结构化正文，作为一篇 ``.md`` 文档入库。

    与 upload 走同一条落库/索引路径（``create_document`` + 后台索引），区别只在
    正文来源是 URL 而不是上传的文件字节。

    出站请求是这个端点唯一的额外攻击面，而它由 egress 兜住：``fetch_page_html``
    走的 ``_http_get_text`` 每一跳（含重定向）都先过 ``egress.check_url``，默认拦
    私网/环回/云元数据。所以这里不需要新开关——它是已鉴权用户主动发起的知识
    管理动作，和上传文件同级，SSRF 由 egress 统一防。

    正文以 ``.md`` 落库：``html_to_text_structured`` 把 ``<h1..6>`` 渲染成 ``#`` 标题、
    块级标签换成换行，于是 chunking 的标题路径与章节边界优先对网页同样生效
    （flatten 成一段就全失效了，症状同 PDF 没恢复结构）。
    """
    url = request.url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"只支持 http/https 链接，收到 {parsed.scheme or '（无协议）'!r}。",
        )

    try:
        raw_html = await workspace_tools.fetch_page_html(url)
    except egress.EgressBlocked as exc:
        raise HTTPException(status_code=400, detail=f"抓取被拒：{exc}")
    except Exception as exc:
        # 超时/连接拒绝/404/SSL/编码不可识别——大多是 URL 本身的问题，归 400
        # 而不是 500：这不是服务故障，换一个 URL 通常就好了。
        raise HTTPException(status_code=400, detail=f"抓取失败：{type(exc).__name__}")

    text = ingest_clean.html_to_text_structured(raw_html)
    if not text.strip():
        raise HTTPException(
            status_code=400,
            detail="该网页没有可入库的正文（可能全是脚本、图片或需要登录）。",
        )

    title = ingest_clean.html_title(raw_html) or parsed.netloc or "web"
    name = workspace_tools.safe_document_name(title) + ".md"

    workspace = workspace_service.resolve_for_user(db, current_user)
    try:
        # 同 upload：只有传共享文档才要 admin，这一句同时挡掉"user 手改请求把
        # visibility 填成 workspace"。
        resolved_visibility = workspace_service.resolve_upload_visibility(
            current_user, request.visibility
        )
    except WorkspaceError as e:
        raise HTTPException(status_code=403, detail=str(e))
    try:
        doc, duplicate = await knowledge_service.create_document(
            db,
            name,
            text.encode("utf-8"),
            workspace_id=workspace.id,
            uploader_id=current_user.id,
            visibility=resolved_visibility,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("URL 文档入库失败")
        raise HTTPException(status_code=500, detail="文档入库失败，请稍后重试") from e

    if not duplicate:
        _kick_indexing(db, background_tasks, doc.id)

    return {
        "id": doc.id,
        "name": doc.name,
        "size": doc.size,
        "chunks": doc.chunks,
        "status": doc.status,
        "visibility": doc.visibility,
        "duplicate": duplicate,
        "sourceUrl": url,
    }


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除文档：共享文档要管理员，个人文档只要本人。

    先取文档再判权限——判据在文档上（``visibility`` 与 ``user_id``），不在角色上。
    404 放在权限判断**之前**：不存在的 id 不该因为"你不是管理员"而被报成 403，
    那会让人以为存在这么一篇文档。

    取的时候带上 admin 的管理可见性，是为了让反过来那一半也诚实：成员的个人文档
    就在 admin 的列表里，对它报 404 是撒谎，而 ``require_can_modify`` 紧接着会给出
    真正的原因（403 "这是他人的个人文档"）。
    """
    workspace = workspace_service.resolve_for_user(db, current_user)
    document = await knowledge_service.find_document(
        db,
        doc_id,
        workspace.id,
        viewer_id=current_user.id,
        include_member_private=workspace_service.is_admin(current_user),
    )
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        workspace_service.require_can_modify(current_user, document)
    except WorkspaceError as e:
        raise HTTPException(status_code=403, detail=str(e))
    deleted = await knowledge_service.delete_document(
        db, doc_id, workspace.id, viewer_id=current_user.id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"success": True}


@router.post("/query")
async def query_knowledge(
    request: QueryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """RAG 检索：工作区共享文档 + 自己的个人文档"""
    workspace = workspace_service.resolve_for_user(db, current_user)
    results = await knowledge_service.search(
        db, request.query, workspace.id, request.top_k, viewer_id=current_user.id
    )
    return {"query": request.query, "results": results, "total": len(results)}


@router.get("/documents/{document_id}/chunks/{chunk_index}")
async def get_document_chunk(
    document_id: str,
    chunk_index: int,
    window: int = 1,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """按 (文档, 分块号) 取原文及相邻分块——引用点击跳原文用（B5）。

    按**当前用户检索得到的范围**收口（共享 + 自己的私有，传 viewer_id）：引用来自
    用户自己那次回答的检索，这里让他回看命中块的上下文，但不能借它去读别人的私有
    文档。窗口夹在 0–5：回看上下文够用，又不至于把半篇文档拉回来。
    """
    workspace = workspace_service.resolve_for_user(db, current_user)
    chunks = await knowledge_service.read_chunks(
        db,
        workspace.id,
        document_id,
        chunk_index,
        window=max(0, min(window, 5)),
        viewer_id=current_user.id,
    )
    if not chunks:
        # 不存在、或不在当前用户可见范围内——都报 404，不泄露"存在但不是你的"
        raise HTTPException(status_code=404, detail="分块不存在或无权查看")
    return {
        "documentId": document_id,
        "documentName": chunks[0]["document_name"],
        "chunks": [
            {"chunkIndex": c["chunk_index"], "content": c["content"]} for c in chunks
        ],
    }
