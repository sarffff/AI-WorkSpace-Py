import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import SessionLocal, get_db
from models import User
from services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["知识库"])
knowledge_service = KnowledgeService()
logger = logging.getLogger("knowledge_router")

# 知识库允许的文件扩展名 (不含 html/svg 等可执行类型)
_KNOWLEDGE_ALLOWED_EXT = {
    "txt", "md", "py", "js", "ts", "tsx", "jsx", "json", "xml", "yaml", "yml",
    "css", "csv", "log", "sh", "java", "go", "rs", "c", "cpp", "pdf",
}


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
    """获取文档列表"""
    return await knowledge_service.get_documents(db, current_user.id)


@router.post("/documents/upload")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上传文档。解析后立即返回 processing，分块与向量化在后台完成。"""
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

    try:
        doc = await knowledge_service.create_document(
            db, file.filename, content, user_id=current_user.id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("文档解析失败")
        raise HTTPException(status_code=500, detail="文档解析失败，请稍后重试") from e

    # 向量化是整个流程里最慢的一环(N 次 embedding 调用),不该占着 HTTP 连接
    background_tasks.add_task(_index_document_task, doc.id)

    return {
        "id": doc.id,
        "name": doc.name,
        "size": doc.size,
        "chunks": doc.chunks,
        "status": doc.status,
    }


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除文档"""
    deleted = await knowledge_service.delete_document(db, doc_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"success": True}


@router.post("/query")
async def query_knowledge(
    request: QueryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """RAG 检索"""
    results = await knowledge_service.search(
        db, request.query, current_user.id, request.top_k
    )
    return {"query": request.query, "results": results, "total": len(results)}
