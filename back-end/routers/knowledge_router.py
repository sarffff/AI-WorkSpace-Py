from fastapi import APIRouter
from services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
knowledge_service = KnowledgeService()


@router.get("/documents")
async def get_documents():
    """获取文档列表（对应 NestJS 的 @Get('documents')）"""
    return await knowledge_service.get_documents()
