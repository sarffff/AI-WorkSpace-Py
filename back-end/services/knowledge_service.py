import io
import re

from sqlalchemy.orm import Session

from models import Document, DocumentChunk
from services.embedding_service import EmbeddingService

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


def _split_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """将文本分割为重叠的块"""
    # 先按段落分割
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) <= chunk_size:
            current = (current + "\n\n" + para).strip() if current else para
        else:
            if current:
                chunks.append(current)
            current = para

    if current:
        chunks.append(current)

    # 如果还有超长块，按字符截断并重叠
    final_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) <= chunk_size:
            final_chunks.append(chunk)
        else:
            start = 0
            while start < len(chunk):
                end = min(start + chunk_size, len(chunk))
                final_chunks.append(chunk[start:end])
                start += chunk_size - overlap

    return final_chunks


def _parse_file_content(filename: str, content: bytes) -> str:
    """根据文件类型解析文本内容"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in ("txt", "md", "py", "js", "ts", "tsx", "jsx", "json", "xml", "yaml", "yml", "css", "html"):
        return content.decode("utf-8", errors="replace")

    if ext == "pdf":
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(content))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            raise ValueError("PDF 解析需要安装 PyPDF2 库")

    raise ValueError(f"不支持的文件格式: .{ext}")


class KnowledgeService:
    def __init__(self):
        self.embedding = EmbeddingService()

    async def get_documents(self, db: Session) -> list[dict]:
        """获取文档列表"""
        documents = db.query(Document).order_by(Document.created_at.desc()).all()
        return [
            {
                "id": doc.id,
                "name": doc.name,
                "size": doc.size,
                "chunks": doc.chunks,
                "status": doc.status,
                "createdAt": doc.created_at.isoformat(),
            }
            for doc in documents
        ]

    async def upload_document(self, db: Session, filename: str, content: bytes, user_id: str | None = None) -> Document:
        """上传并索引文档"""
        # 1. 解析文件内容
        text = _parse_file_content(filename, content)

        # 2. 创建文档记录
        doc = Document(
            name=filename,
            size=len(content),
            content=text,
            user_id=user_id,
            status="processing",
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)

        try:
            # 3. 分块
            chunks = _split_text(text)
            if not chunks:
                doc.status = "indexed"
                doc.chunks = 0
                db.commit()
                return doc

            # 4. 批量向量化
            embeddings = await self.embedding.embed_texts(chunks)

            # 5. 保存分块和向量
            for i, (chunk_text, vec) in enumerate(zip(chunks, embeddings)):
                chunk = DocumentChunk(
                    document_id=doc.id,
                    content=chunk_text,
                    embedding=EmbeddingService.serialize(vec),
                    chunk_index=i,
                )
                db.add(chunk)

            doc.chunks = len(chunks)
            doc.status = "indexed"
            db.commit()

        except Exception:
            doc.status = "failed"
            db.commit()
            raise

        return doc

    async def search(self, db: Session, query: str, top_k: int = 5) -> list[dict]:
        """RAG 检索：查询知识库中最相关的文本块"""
        # 1. 向量化查询
        query_vec = await self.embedding.embed_query(query)
        if not query_vec:
            return []

        # 2. 加载所有分块并计算相似度
        all_chunks = db.query(DocumentChunk).all()
        scored = []
        for chunk in all_chunks:
            if not chunk.embedding:
                continue
            vec = EmbeddingService.deserialize(chunk.embedding)
            sim = EmbeddingService.cosine_similarity(query_vec, vec)
            scored.append((sim, chunk))

        # 3. 排序取 top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        return [
            {
                "id": chunk.id,
                "document_id": chunk.document_id,
                "content": chunk.content,
                "score": round(score, 4),
            }
            for score, chunk in top
        ]

    async def delete_document(self, db: Session, doc_id: str) -> bool:
        """删除文档及其所有分块"""
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            return False
        db.delete(doc)  # cascade 会自动删除关联的 chunks
        db.commit()
        return True

    async def build_rag_context(self, db: Session, query: str, top_k: int = 5) -> str:
        """构建 RAG 上下文，返回格式化的文本"""
        results = await self.search(db, query, top_k)
        if not results:
            return ""

        parts = ["以下是知识库中与当前问题相关的参考内容：\n"]
        for i, r in enumerate(results, 1):
            parts.append(f"【参考 {i}】(相关度: {r['score']})\n{r['content']}\n")

        return "\n".join(parts)