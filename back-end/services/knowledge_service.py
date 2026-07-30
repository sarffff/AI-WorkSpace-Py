from sqlalchemy.orm import Session
from models import Document


class KnowledgeService:
    async def get_documents(self, db: Session) -> list[dict]:
        """获取文档列表（从数据库）"""
        documents = db.query(Document).order_by(Document.created_at.desc()).all()
        return [
            {
                "id": doc.id,
                "name": doc.name,
                "size": doc.size,
                "chunks": doc.chunks,
                "status": doc.status,
            }
            for doc in documents
        ]
