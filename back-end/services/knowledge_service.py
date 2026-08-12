"""知识库文档的解析、入库与检索入口。

索引结构与检索算法在 ``retrieval_index`` / ``retriever``，分块在 ``chunking``；
这里只负责文档生命周期：解析 -> 落库 -> 分块向量化 -> 检索 -> 删除。
"""
from __future__ import annotations

import io
import logging

from sqlalchemy.orm import Session

from config import settings
from models import Document, DocumentChunk
from services.chunking import split_document
from services.embedding_service import EmbeddingService
from services.retrieval_index import invalidate_user_indexes
from services.retriever import HybridRetriever, RetrievedChunk, format_context
from services.token_budget import get_token_counter

logger = logging.getLogger("knowledge_service")

_TEXT_EXTENSIONS = {
    "txt", "md", "js", "ts", "tsx", "jsx", "json", "xml", "yaml", "yml",
    "css", "csv", "log", "sh", "java", "go", "rs", "c", "cpp", "py",
}


def _parse_file_content(filename: str, content: bytes) -> str:
    """根据文件类型解析文本内容。不支持的格式抛 ValueError,由路由转成 400。"""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if extension in _TEXT_EXTENSIONS:
        return content.decode("utf-8", errors="replace")

    if extension == "pdf":
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise ValueError("PDF 解析需要安装 PyPDF2 库")
        reader = PdfReader(io.BytesIO(content))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)

    raise ValueError(f"不支持的文件格式: .{extension}")


class KnowledgeService:
    def __init__(self, retriever: HybridRetriever | None = None) -> None:
        self.embedding = EmbeddingService()
        self.retriever = retriever or HybridRetriever(embedding=self.embedding)

    async def get_documents(self, db: Session, user_id: str) -> list[dict]:
        documents = (
            db.query(Document)
            .filter(Document.user_id == user_id)
            .order_by(Document.created_at.desc())
            .all()
        )
        return [
            {
                "id": document.id,
                "name": document.name,
                "size": document.size,
                "chunks": document.chunks,
                "status": document.status,
                "createdAt": document.created_at.isoformat(),
            }
            for document in documents
        ]

    async def create_document(
        self, db: Session, filename: str, content: bytes, user_id: str
    ) -> Document:
        """解析并落库，状态置 processing。分块与向量化交给 index_document。"""
        text = _parse_file_content(filename, content)
        document = Document(
            name=filename,
            size=len(content),
            content=text,
            user_id=user_id,
            status="processing",
        )
        db.add(document)
        db.commit()
        db.refresh(document)
        return document

    async def index_document(self, db: Session, document_id: str) -> None:
        """分块 + 批量向量化 + 落库。

        供后台任务调用，所以内部吞掉异常并把文档标成 failed —— 后台任务抛出的
        异常没有接收方，状态机才是用户能看到的反馈。
        """
        document = db.query(Document).filter(Document.id == document_id).first()
        if not document:
            logger.warning("Document %s disappeared before indexing", document_id)
            return

        try:
            chunks = split_document(
                document.content or "",
                document.name,
                max_tokens=settings.CHUNK_MAX_TOKENS,
                overlap_tokens=settings.CHUNK_OVERLAP_TOKENS,
                counter=get_token_counter(settings.TOKEN_COUNTER),
            )
            if not chunks:
                document.status = "indexed"
                document.chunks = 0
                db.commit()
                return

            embeddings = await self.embedding.embed_texts(
                [chunk.content for chunk in chunks]
            )
            if len(embeddings) != len(chunks):
                raise RuntimeError(
                    f"Embedding 数量不匹配：期望 {len(chunks)}，实际 {len(embeddings)}"
                )

            for chunk, vector in zip(chunks, embeddings):
                db.add(
                    DocumentChunk(
                        document_id=document.id,
                        content=chunk.content,
                        embedding=EmbeddingService.serialize(
                            vector, model=settings.EMBEDDING_MODEL
                        ),
                        chunk_index=chunk.index,
                    )
                )
            document.chunks = len(chunks)
            document.status = "indexed"
            db.commit()
            invalidate_user_indexes(document.user_id)
        except Exception:
            logger.exception("Indexing failed for document %s", document_id)
            db.rollback()
            failed = db.query(Document).filter(Document.id == document_id).first()
            if failed:
                failed.status = "failed"
                db.commit()

    async def upload_document(
        self, db: Session, filename: str, content: bytes, user_id: str
    ) -> Document:
        """同步上传：解析、落库、立即索引。上传接口现在走异步路径，这里保留给脚本使用。"""
        document = await self.create_document(db, filename, content, user_id)
        await self.index_document(db, document.id)
        db.refresh(document)
        return document

    async def retrieve(
        self, db: Session, query: str, user_id: str, top_k: int = 5
    ) -> list[RetrievedChunk]:
        return await self.retriever.retrieve(db, user_id, query, top_k=top_k)

    async def search(
        self, db: Session, query: str, user_id: str, top_k: int = 5
    ) -> list[dict]:
        """检索接口，返回可直接 JSON 序列化的结果。"""
        chunks = await self.retrieve(db, query, user_id, top_k)
        return [chunk.as_dict() for chunk in chunks]

    async def build_rag_context_with_citations(
        self, db: Session, query: str, user_id: str, top_k: int = 5
    ) -> tuple[str, list[dict]]:
        """返回 (喂给模型的参考内容, 结构化引用列表)。"""
        chunks = await self.retrieve(db, query, user_id, top_k)
        return format_context(chunks), [chunk.as_dict() for chunk in chunks]

    async def build_rag_context(
        self, db: Session, query: str, user_id: str, top_k: int = 5
    ) -> str:
        context, _citations = await self.build_rag_context_with_citations(
            db, query, user_id, top_k
        )
        return context

    async def read_chunks(
        self,
        db: Session,
        user_id: str,
        document_id: str,
        chunk_index: int,
        window: int = 1,
    ) -> list[dict]:
        """读取指定分块及其相邻分块，仅返回归属于 user_id 的文档。"""
        document = (
            db.query(Document)
            .filter(Document.id == document_id, Document.user_id == user_id)
            .first()
        )
        if not document:
            return []

        chunks = (
            db.query(DocumentChunk)
            .filter(
                DocumentChunk.document_id == document_id,
                DocumentChunk.chunk_index >= max(0, chunk_index - window),
                DocumentChunk.chunk_index <= chunk_index + window,
            )
            .order_by(DocumentChunk.chunk_index.asc())
            .all()
        )
        return [
            {
                "document_name": document.name,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
            }
            for chunk in chunks
        ]

    async def delete_document(self, db: Session, doc_id: str, user_id: str) -> bool:
        """删除文档及其所有分块（cascade），并让该用户的索引失效。"""
        document = (
            db.query(Document)
            .filter(Document.id == doc_id, Document.user_id == user_id)
            .first()
        )
        if not document:
            return False
        db.delete(document)
        db.commit()
        invalidate_user_indexes(user_id)
        return True
