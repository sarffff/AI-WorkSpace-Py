from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["知识库"])
knowledge_service = KnowledgeService()


class QueryRequest(BaseModel):
    query: str
    top_k: int = 5


@router.get("/documents")
async def get_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取文档列表"""
    return await knowledge_service.get_documents(db)


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上传文档并自动索引"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    content = await file.read()
    max_size = 10 * 1024 * 1024  # 10MB
    if len(content) > max_size:
        raise HTTPException(status_code=400, detail=f"文件大小不能超过 {max_size // 1024 // 1024}MB")

    try:
        doc = await knowledge_service.upload_document(db, file.filename, content, user_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="文档处理失败，请稍后重试")

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
    deleted = await knowledge_service.delete_document(db, doc_id)
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
    results = await knowledge_service.search(db, request.query, request.top_k)
    return {"query": request.query, "results": results, "total": len(results)}