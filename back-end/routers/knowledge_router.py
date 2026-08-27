import logging

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
from services import file_types, workspace_service
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


async def _index_document_task(document_id: str) -> None:
    """后台索引任务。

    必须自建 session：请求作用域的 session 在响应返回时就被 get_db 关掉了。
    index_document 内部自行处理失败并把文档标成 failed。
    """
    db = SessionLocal()
    try:
        await knowledge_service.index_document(db, document_id)
    finally:
        db.close()


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
    return await knowledge_service.get_documents(
        db,
        workspace.id,
        viewer_id=current_user.id,
        include_member_private=workspace_service.is_admin(current_user),
    )


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
        background_tasks.add_task(_index_document_task, doc.id)

    return {
        "id": doc.id,
        "name": doc.name,
        "size": doc.size,
        "chunks": doc.chunks,
        "status": doc.status,
        "visibility": doc.visibility,
        "duplicate": duplicate,
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
