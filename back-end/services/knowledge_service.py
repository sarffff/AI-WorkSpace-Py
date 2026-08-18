"""知识库文档的解析、入库与检索入口。

索引结构与检索算法在 ``retrieval_index`` / ``retriever``，分块在 ``chunking``；
这里只负责文档生命周期：解析 -> 落库 -> 分块向量化 -> 检索 -> 删除。
"""
from __future__ import annotations

import hashlib
import io
import logging

from sqlalchemy.orm import Session

from config import settings
from models import Document, DocumentChunk
from services.chunking import split_document
from services.embedding_service import EmbeddingService
from services.retrieval_index import invalidate_scope_indexes
from services.semantic_cache import semantic_cache
from services.retriever import HybridRetriever, RetrievedChunk, format_context
from services.token_budget import get_token_counter

logger = logging.getLogger("knowledge_service")

TEXT_EXTENSIONS = {
    "txt", "md", "js", "ts", "tsx", "jsx", "json", "xml", "yaml", "yml",
    "css", "csv", "log", "sh", "java", "go", "rs", "c", "cpp", "py",
}


def parse_file_content(filename: str, content: bytes) -> str:
    """根据文件类型解析文本内容。不支持的格式抛 ValueError,由路由转成 400。

    公开而不是私有:上传链路和 Agent 的 read_attachment 工具必须用同一套解析,
    各写一份的结果是"同一个 PDF 进知识库能读、当附件读不了"这类难查的不一致。
    """
    base_name = filename.split("#", 1)[0]
    extension = base_name.rsplit(".", 1)[-1].lower() if "." in base_name else ""

    if extension in TEXT_EXTENSIONS:
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

    async def get_documents(self, db: Session, workspace_id: str) -> list[dict]:
        documents = (
            db.query(Document)
            .filter(Document.workspace_id == workspace_id)
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
        self, db: Session, filename: str, content: bytes, workspace_id: str,
        uploader_id: str | None = None,
    ) -> tuple[Document, bool]:
        """解析并落库，状态置 processing。分块与向量化交给 index_document。

        返回 (document, duplicate)。按解析后正文的哈希做幂等去重:同一用户
        重复上传同一内容时返回已有文档,不再多建一套 chunk——重复文档会在
        检索时占据 top_k 的多个席位,把其他文档挤出去。唯一例外是已有文档
        状态为 failed:那说明上次索引没建成,删掉重建等于给了重试入口。
        """
        text = parse_file_content(filename, content)
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        existing = (
            db.query(Document)
            .filter(
                Document.workspace_id == workspace_id,
                Document.content_hash == content_hash,
            )
            .first()
        )
        if existing is not None:
            if existing.status != "failed":
                return existing, True
            # failed 的旧文档没有任何可复用的资产,直接删掉走全新入库
            db.delete(existing)
            db.commit()

        document = Document(
            name=filename,
            size=len(content),
            content=text,
            workspace_id=workspace_id,
            user_id=uploader_id,
            status="processing",
            content_hash=content_hash,
        )
        db.add(document)
        db.commit()
        db.refresh(document)
        return document, False

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
            # 知识库变了，旧答案可能已经错了：整桶清掉而不是逐条判断。
            # 作用域是工作区:一个成员上传,全体成员的缓存都要失效
            invalidate_scope_indexes(document.workspace_id)
            semantic_cache.invalidate_user(document.workspace_id)
        except Exception:
            logger.exception("Indexing failed for document %s", document_id)
            db.rollback()
            failed = db.query(Document).filter(Document.id == document_id).first()
            if failed:
                failed.status = "failed"
                db.commit()

    async def upload_document(
        self, db: Session, filename: str, content: bytes, workspace_id: str,
        uploader_id: str | None = None,
    ) -> Document:
        """同步上传：解析、落库、立即索引。上传接口现在走异步路径，这里保留给脚本使用。"""
        document, _duplicate = await self.create_document(db, filename, content, workspace_id, uploader_id)
        await self.index_document(db, document.id)
        db.refresh(document)
        return document

    async def retrieve(
        self, db: Session, query: str, workspace_id: str, top_k: int = 5
    ) -> list[RetrievedChunk]:
        return await self.retriever.retrieve(db, workspace_id, query, top_k=top_k)

    async def search(
        self, db: Session, query: str, workspace_id: str, top_k: int = 5
    ) -> list[dict]:
        """检索接口，返回可直接 JSON 序列化的结果。"""
        chunks = await self.retrieve(db, query, workspace_id, top_k)
        return [chunk.as_dict() for chunk in chunks]

    async def build_rag_context_with_citations(
        self, db: Session, query: str, workspace_id: str, top_k: int = 5
    ) -> tuple[str, list[dict]]:
        """返回 (喂给模型的参考内容, 结构化引用列表)。"""
        chunks = await self.retrieve(db, query, workspace_id, top_k)
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
        workspace_id: str,
        document_id: str,
        chunk_index: int,
        window: int = 1,
    ) -> list[dict]:
        """读取指定分块及其相邻分块，仅返回归属于该工作区的文档。"""
        document = (
            db.query(Document)
            .filter(Document.id == document_id, Document.workspace_id == workspace_id)
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

    async def delete_document(self, db: Session, doc_id: str, workspace_id: str) -> bool:
        """删除文档及其所有分块（cascade），并让该工作区的索引失效。"""
        document = (
            db.query(Document)
            .filter(Document.id == doc_id, Document.workspace_id == workspace_id)
            .first()
        )
        if not document:
            return False
        db.delete(document)
        db.commit()
        invalidate_scope_indexes(workspace_id)
        semantic_cache.invalidate_user(workspace_id)
        return True
