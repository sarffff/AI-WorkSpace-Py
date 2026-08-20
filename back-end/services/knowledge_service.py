"""知识库文档的解析、入库与检索入口。

索引结构与检索算法在 ``retrieval_index`` / ``retriever``，分块在 ``chunking``，
解码与 PDF 结构恢复在 ``ingest_clean``；这里只负责文档生命周期：
解析 -> 落库 -> 分块向量化 -> 自检 -> 检索 -> 删除。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from config import settings
from models import Document, DocumentChunk
from services import ingest_clean
from services import chunking
from services import vector_store
from services.chunking import Chunk, split_document
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


@dataclass(slots=True)
class ParsedDocument:
    """解析结果。``warnings`` 是给入库自检和界面看的溯源信息。

    带上 ``backend`` 与 ``warnings`` 的理由：解析失败的那几种最常见形态
    （扫描件、编码错误、没识别出标题）**都不抛异常**，只表现为"检索不到"。
    没有这两个字段时，排查只能靠肉眼比对分块内容。
    """

    text: str
    backend: str = "text"
    warnings: list[str] = field(default_factory=list)


def parse_document(filename: str, content: bytes) -> ParsedDocument:
    """按文件类型解析，并带回解析后端与告警。不支持的格式抛 ValueError。

    公开而不是私有：上传链路和 Agent 的 read_attachment 工具必须用同一套解析，
    各写一份的结果是"同一个 PDF 进知识库能读、当附件读不了"这类难查的不一致。
    """
    base_name = filename.split("#", 1)[0]
    extension = base_name.rsplit(".", 1)[-1].lower() if "." in base_name else ""

    if extension in TEXT_EXTENSIONS:
        if not settings.INGEST_CLEAN:
            # 对照组：改动前的行为。一份 GBK 文档在这条路径上会变成一串 U+FFFD，
            # 然后被 BM25 完全跳过——这正是 dirty-gbk 变体要量的那个差值。
            return ParsedDocument(
                text=content.decode("utf-8", errors="replace"), backend="raw"
            )
        decoded = ingest_clean.sniff_decode(content)
        warnings: list[str] = []
        if decoded.encoding != "utf-8":
            warnings.append(f"decoded_as:{decoded.encoding}")
        if decoded.replacement_ratio < 1.0:
            warnings.append(f"readable_ratio:{decoded.replacement_ratio:.2f}")
        return ParsedDocument(
            text=ingest_clean.clean_text(decoded.text),
            backend=f"text/{decoded.encoding}",
            warnings=warnings,
        )

    if extension == "pdf":
        if settings.INGEST_PDF_STRUCTURE:
            extraction = ingest_clean.extract_pdf(content)
        else:
            extraction = ingest_clean.extract_pdf_plain(content, [])
        return ParsedDocument(
            text=extraction.text,
            backend=extraction.backend,
            warnings=list(extraction.warnings),
        )

    raise ValueError(f"不支持的文件格式: .{extension}")


def parse_file_content(filename: str, content: bytes) -> str:
    """只要正文的调用方用这个（read_attachment 工具）。

    薄封装而不是各自实现：解析必须只有一处，否则两条路径会慢慢分叉。
    """
    return parse_document(filename, content).text


def _dump_warnings(warnings: list[str]) -> str | None:
    """告警列表落库成 JSON。空列表存 NULL 而不是 ``"[]"``——查询时
    ``parse_warnings IS NOT NULL`` 就等于"这篇文档有话要说"。"""
    unique = list(dict.fromkeys(warning for warning in warnings if warning))
    return json.dumps(unique, ensure_ascii=False) if unique else None


def _load_warnings(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []



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
                # 解析后端与告警一起返回:一篇 failed 的文档光看状态说不清是编码坏了、
                # 是扫描件还是 embedding 挂了,而这三者的处理办法完全不同
                "parseBackend": document.parse_backend,
                "parseWarnings": _load_warnings(document.parse_warnings),
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
        parsed = parse_document(filename, content)
        content_hash = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()

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
            content=parsed.text,
            workspace_id=workspace_id,
            user_id=uploader_id,
            status="processing",
            content_hash=content_hash,
            parse_backend=parsed.backend[:32],
            parse_warnings=_dump_warnings(parsed.warnings),
        )
        db.add(document)
        db.commit()
        db.refresh(document)
        return document, False

    async def index_document(self, db: Session, document_id: str) -> None:
        """分块 + 批量向量化 + 落库 + 入库自检。

        供后台任务调用，所以内部吞掉异常并把文档标成 failed —— 后台任务抛出的
        异常没有接收方，状态机才是用户能看到的反馈。

        自检的存在理由：这条链路上最常见的几种失败**全都不抛异常**——扫描件抽出
        空文本、GBK 文档解成一串替换符、文档全是图表没有可切的正文。改动前它们
        一律落成 ``status="indexed"``，界面上和一篇正常文档毫无区别，只是永远
        检索不到。所以判据必须从"没抛异常"换成"真的能检索到"。
        """
        document = db.query(Document).filter(Document.id == document_id).first()
        if not document:
            logger.warning("Document %s disappeared before indexing", document_id)
            return

        warnings = _load_warnings(document.parse_warnings)
        try:
            text = document.content or ""
            # 自检 1：可读字符占比。放在分块之前——一份乱码文档照样能切出
            # 几十个块、每个块都能算出向量，走到最后一步才发现已经白花了钱。
            ratio = ingest_clean.readable_ratio(text)
            if ratio < settings.INGEST_MIN_TEXT_RATIO:
                self._fail(
                    db, document, warnings,
                    f"unreadable_text:{ratio:.2f}<{settings.INGEST_MIN_TEXT_RATIO}",
                )
                return

            chunks = await self._split(text, document.name, warnings)
            # 自检 2：切不出块。改动前这里是 status="indexed" + chunks=0，
            # 于是扫描版 PDF 会静默地变成一篇"存在但检索不到"的文档。
            if not chunks:
                self._fail(db, document, warnings, "no_chunks")
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
            document.parse_warnings = _dump_warnings(warnings)
            db.commit()
            # 知识库变了，旧答案可能已经错了：整桶清掉而不是逐条判断。
            # 作用域是工作区:一个成员上传,全体成员的缓存都要失效
            invalidate_scope_indexes(document.workspace_id)
            semantic_cache.invalidate_user(document.workspace_id)

            # 向量库是**持久态**,必须在这里同步写。进程内索引是派生态、丢了重建
            # 就行,而 Qdrant 里少一批点就是真的少了——靠下一次检索去兜是兜不住的,
            # 因为检索侧不知道"哪些该在里面"。memory 后端下这一步是空操作。
            await self._sync_vectors(db, document)
            await self._self_check_retrieval(db, document, chunks)
        except Exception:
            logger.exception("Indexing failed for document %s", document_id)
            db.rollback()
            failed = db.query(Document).filter(Document.id == document_id).first()
            if failed:
                failed.status = "failed"
                failed.parse_warnings = _dump_warnings(
                    _load_warnings(failed.parse_warnings) + ["indexing_exception"]
                )
                db.commit()

    async def _split(
        self, text: str, filename: str, warnings: list[str]
    ) -> list[Chunk]:
        """按 ``CHUNK_STRATEGY`` 分块。语义分块失败一律退回结构分块。

        语义分块的成本要说清楚：它先把正文切成句子、批量向量化一次求断点，之后
        分好的块**还要再向量化一次**入库。也就是入库时的 embedding 调用量大约翻倍。
        这是所有语义分块实现共同的代价（句向量用来找边界，块向量用来建索引），
        不是这里写得不好——但它只发生在入库时，检索侧一分不多花。
        """
        counter = get_token_counter(settings.TOKEN_COUNTER)
        structural = lambda: split_document(  # noqa: E731
            text,
            filename,
            max_tokens=settings.CHUNK_MAX_TOKENS,
            overlap_tokens=settings.CHUNK_OVERLAP_TOKENS,
            counter=counter,
        )
        if settings.CHUNK_STRATEGY != "semantic":
            return structural()

        units = chunking.sentences_for_embedding(text, filename)
        # 句子太少时距离分布没有统计意义:三句话的 95 分位数就是最大值,
        # "只在最跳的 5% 处断开"退化成"在唯一那个间隙处断开"
        if len(units) < settings.CHUNK_SEMANTIC_MIN_SENTENCES:
            warnings.append(f"semantic_fallback:too_few_units:{len(units)}")
            return structural()

        try:
            vectors = await self.embedding.embed_texts([unit.text for unit in units])
        except Exception as exc:
            # 分块策略不该成为单点故障:embedding 挂了就退回结构分块,
            # 文档照样进库,只是断点判据差一些
            logger.warning(
                "semantic chunking embedding failed (%s); falling back",
                type(exc).__name__,
            )
            warnings.append(f"semantic_fallback:{type(exc).__name__}")
            return structural()

        if len(vectors) != len(units):
            warnings.append("semantic_fallback:vector_count_mismatch")
            return structural()

        chunks = chunking.split_semantic(
            units,
            chunking.adjacent_distances(vectors),
            max_tokens=settings.CHUNK_MAX_TOKENS,
            percentile=settings.CHUNK_SEMANTIC_PERCENTILE,
            counter=counter,
        )
        if not chunks:
            warnings.append("semantic_fallback:no_chunks")
            return structural()
        warnings.append(f"semantic_chunking:{len(units)}units")
        return chunks

    @staticmethod
    async def _sync_vectors(db: Session, document: Document) -> None:
        """把这篇文档的向量写进向量库。memory 后端下是空操作。

        从库里重新读一遍 chunk 而不是复用刚才那批 vector：point id 必须是
        ``DocumentChunk.id``，而那个 id 是 ``db.commit()`` 时才由默认值生成的，
        提交前手上只有内容和向量、没有主键。
        """
        store = vector_store.get_store()
        if not vector_store.uses_qdrant():
            return
        rows = (
            db.query(DocumentChunk)
            .filter(DocumentChunk.document_id == document.id)
            .all()
        )
        records = []
        for row in rows:
            try:
                vector = EmbeddingService.deserialize(row.embedding or "")
            except (TypeError, ValueError):
                continue
            if vector:
                records.append(
                    vector_store.VectorRecord(
                        chunk_id=row.id, document_id=document.id, vector=vector
                    )
                )
        if not records:
            return
        await store.upsert(document.workspace_id, records)

    @staticmethod
    def _fail(
        db: Session, document: Document, warnings: list[str], reason: str
    ) -> None:
        """把文档判为 failed 并记下原因。

        原因必须落库：``failed`` 单独一个状态只说明"没建成"，说不清是编码坏了、
        是扫描件、还是 embedding 接口挂了——而这三者的处理办法完全不同。
        """
        document.status = "failed"
        document.chunks = 0
        document.parse_warnings = _dump_warnings(warnings + [reason])
        db.commit()
        logger.warning(
            "Ingest self-check rejected document %s: %s", document.id, reason
        )

    async def _self_check_retrieval(
        self, db: Session, document: Document, chunks: list
    ) -> None:
        """抽一块的正文当查询检索一次，命中不了自己就记 warning。

        为什么这条值得多花一次 embedding 调用：前两条自检查的是"文本对不对"，
        这条查的是"整条链路通不通"——维度不匹配、embedding 模型换了没重建索引、
        作用域串了，这些全都不抛异常，也全都表现为"检索不到"。用文档自己的
        分块当查询是最强的一个正例：它连不上自己，别的查询更不可能连上。
        """
        if not settings.INGEST_SELF_CHECK or not chunks:
            return
        # 取中间那一块而不是第一块：第一块常常只是标题页
        probe = chunks[len(chunks) // 2].content[:200]
        if not probe.strip():
            return
        try:
            hits = await self.retrieve(db, probe, document.workspace_id, top_k=5)
        except Exception as exc:
            logger.warning(
                "self-check retrieval failed for %s: %s",
                document.id,
                type(exc).__name__,
            )
            return
        if any(hit.document_id == document.id for hit in hits):
            return
        warnings = _load_warnings(document.parse_warnings) + ["self_check_miss"]
        document.parse_warnings = _dump_warnings(warnings)
        db.commit()
        logger.warning(
            "Document %s cannot retrieve its own chunk; index may be broken",
            document.id,
        )

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
        # MySQL 那边是 ON DELETE CASCADE，Qdrant 没有外键，所以这一步必须显式做。
        # 漏掉的后果是删掉的文档还能被召回，而 retriever 的 `if chunk_id in by_id`
        # 会把它静默丢掉——表现是 top_k 少了几条，没有任何报错。
        if vector_store.uses_qdrant():
            try:
                await vector_store.get_store().delete_document(workspace_id, doc_id)
            except Exception as exc:
                logger.error(
                    "failed to delete vectors for %s from the vector store: %s",
                    doc_id,
                    type(exc).__name__,
                )
        return True
