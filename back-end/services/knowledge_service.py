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
from models import Document, DocumentChunk, User
from services import file_types
from services import workspace_service
from services.workspace_service import VISIBILITY_WORKSPACE
from services import ingest_clean
from services import chunking
from services import vector_store
from services.chunking import Chunk, split_document
from services.embedding_service import EmbeddingService
from services.retrieval_index import (
    invalidate_scope_indexes,
    invalidate_viewer_indexes,
)
from services.semantic_cache import semantic_cache
from services.retriever import HybridRetriever, RetrievedChunk, format_context
from services.token_budget import get_token_counter

logger = logging.getLogger("knowledge_service")

# 从 file_types 派生而不是自己列一份：这份清单此前有六处副本且已互相矛盾，
# 理由与代价见 services/file_types.py 的模块文档。保留这个名字是因为
# parse_document 用它做分派，改名对调用方没有收益。
TEXT_EXTENSIONS = file_types.TEXT


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

    if extension in ("docx", "xlsx"):
        # 和 PDF 走同一个返回类型与同一套 warning 词汇，所以这里只是转形状。
        # 没有 INGEST_* 开关对照组：PDF 那个开关存在是因为"结构恢复"是启发式的
        # （字号猜层级），需要一个能关掉的对照。docx 的层级在样式名里是显式的，
        # 没有什么可对照的——加一个恒定更差的分支只会多一条没人跑的路径。
        if extension == "docx":
            extraction = ingest_clean.extract_docx(content)
        else:
            extraction = ingest_clean.extract_xlsx(content)
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

    async def get_documents(
        self,
        db: Session,
        workspace_id: str,
        viewer_id: str | None = None,
        *,
        include_member_private: bool = False,
    ) -> list[dict]:
        """列出可见文档：工作区共享 + 自己的私有（+ admin 看到成员的私有）。

        这是**管理列表**,范围由 ``workspace_service.listable_documents`` 定义,而它
        和检索作用域(``HybridRetriever._retrievable_by``)从 2026-08-24 起有意不同:
        ``include_member_private=True`` 时 admin 会多列出成员的私有文档,那些文档
        **不进他的检索**。所以每一行都带 ``retrievable``,让界面能把"看得见"和
        "会被引用"分开说——两者混在一起时,admin 看到一堆文档却问不出内容,
        只会以为检索坏了。

        ``include_member_private`` 默认 False:漏传只是 admin 少看几篇,反过来则是
        把私有文档列给普通成员。调用方按 ``workspace_service.is_admin(user)`` 传。
        """
        query = (
            db.query(Document, User.name, User.username, User.email)
            # outerjoin 而不是 join:``user_id`` 可空(旧数据、以及删用户时
            # ondelete="SET NULL"),内连接会把那些文档整行从列表里吃掉——
            # 而共享文档丢失一行比拿不到上传者名字严重得多。
            .outerjoin(User, Document.user_id == User.id)
            .filter(
                workspace_service.listable_documents(
                    workspace_id,
                    viewer_id,
                    include_member_private=include_member_private,
                )
            )
            .order_by(Document.created_at.desc())
        )
        rows = query.all()
        return [
            {
                "id": document.id,
                "name": document.name,
                "size": document.size,
                "chunks": document.chunks,
                "status": document.status,
                # 可见性与归属:界面要据此显示"共享/个人"标记,并决定删除按钮
                # 给不给点。isOwn 由后端算而不是前端比 user_id——前端手上不一定
                # 有当前用户 id,而这个判断错了就是一个能点但会 403 的按钮。
                "visibility": document.visibility,
                "isOwn": bool(viewer_id) and document.user_id == viewer_id,
                # 上传者显示名。工作区成员列表里本来就有全体成员的名字,所以这
                # 不是新泄露的信息;它的用处是 admin 在列表里看到一篇别人的私有
                # 文档时,知道该找谁,而不是只看到一个不能删的文件名。
                "ownerName": name or username or email,
                # 原上传者的账号已被删除,这篇是被收编成共享的
                # (workspace_service.adopt_orphaned_documents)。判据是"共享但没有
                # 上传者"——正常上传总会带 uploader_id,所以这个组合只可能来自收编。
                # 界面据此打「继承」标记:admin 需要知道这不是团队有意发布的资料,
                # 而是某个离开的人留下的,值得看一眼再决定删或留。
                "inherited": document.visibility == VISIBILITY_WORKSPACE
                and document.user_id is None,
                # 这一篇会不会进**当前查看者**的检索。admin 列表里成员的私有文档
                # 是 False——它可见但不参与他的回答。判据必须和
                # HybridRetriever._retrievable_by 一致,test_document_visibility
                # 里有一条断言拿真实检索结果把它对上。
                "retrievable": document.visibility == VISIBILITY_WORKSPACE
                or (bool(viewer_id) and document.user_id == viewer_id),
                # 解析后端与告警一起返回:一篇 failed 的文档光看状态说不清是编码坏了、
                # 是扫描件还是 embedding 挂了,而这三者的处理办法完全不同
                "parseBackend": document.parse_backend,
                "parseWarnings": _load_warnings(document.parse_warnings),
                "createdAt": document.created_at.isoformat(),
            }
            for document, name, username, email in rows
        ]

    async def create_document(
        self, db: Session, filename: str, content: bytes, workspace_id: str,
        uploader_id: str | None = None,
        visibility: str = "workspace",
    ) -> tuple[Document, bool]:
        """解析并落库，状态置 processing。分块与向量化交给 index_document。

        返回 (document, duplicate)。按解析后正文的哈希做幂等去重:同一用户
        重复上传同一内容时返回已有文档,不再多建一套 chunk——重复文档会在
        检索时占据 top_k 的多个席位,把其他文档挤出去。唯一例外是已有文档
        状态为 failed:那说明上次索引没建成,删掉重建等于给了重试入口。

        **去重范围含可见性作用域**:共享文档按整个工作区去重,私有文档按上传者
        本人去重。只按 (工作区, 哈希) 去重会让第二个人上传同一份文件时拿到
        **第一个人的私有文档**——既是越权也是错误的复用。而共享与私有之间不去重:
        同一份内容既是团队资产又是某人的私有副本,是两篇不同归属的文档。
        """
        parsed = parse_document(filename, content)
        content_hash = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()

        duplicate_query = db.query(Document).filter(
            Document.workspace_id == workspace_id,
            Document.content_hash == content_hash,
            Document.visibility == visibility,
        )
        if visibility == "private":
            duplicate_query = duplicate_query.filter(Document.user_id == uploader_id)
        existing = duplicate_query.first()
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
            visibility=visibility,
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

            # 先删掉这篇文档已有的分块,再写新的。少了这一步,重新索引就是
            # **追加**:分块数翻倍,而 document.chunks 只记本次的数量,于是元数据
            # 说 7、库里有 14。它不抛异常、不改状态,只让重复分块挤占 top_k——
            # 2026-08-23 在 eval 语料上实测 6 篇老文档全部翻倍(14/7、10/5),
            # 喂给重排的 20 条候选里 6 条是重复的。见 tests/test_reindex_duplicates.py
            #
            # 放在 embedding 之后:向量化失败时走 except 分支回滚,旧分块还在,
            # 文档退回 failed 但检索不至于中途变空。
            db.query(DocumentChunk).filter(
                DocumentChunk.document_id == document.id
            ).delete(synchronize_session=False)

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
            # 作用域是工作区:一个成员上传共享文档,全体成员的缓存都要失效
            invalidate_scope_indexes(document.workspace_id)
            semantic_cache.invalidate_user(document.workspace_id)
            # 私有文档还要按**人**清一次:它跟着人走,而这个人此刻所在的工作区
            # 未必是文档的 workspace_id(可能是他在上一个空间里传的)。
            # 漏掉的症状是刚传完的个人文档搜不到,而它明明 indexed 了。
            if document.visibility == "private" and document.user_id:
                invalidate_viewer_indexes(document.user_id)
                semantic_cache.invalidate_viewer(document.user_id)

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
        visibility: str = "workspace",
    ) -> Document:
        """同步上传：解析、落库、立即索引。上传接口现在走异步路径，这里保留给脚本使用。

        命中内容去重且已索引好时**不再重建索引**。``create_document`` 的去重只
        避免了多建一行 Document,如果这里照旧调 ``index_document``,分块仍然会
        被重算一遍——修好追加问题之后那不再翻倍,但它是一次白花的 embedding
        调用(``ensure_corpus`` 每次重建对 13 篇全量重传,等于整批白算)。
        """
        document, duplicate = await self.create_document(
            db, filename, content, workspace_id, uploader_id, visibility=visibility
        )
        if duplicate and document.status == "indexed":
            return document
        await self.index_document(db, document.id)
        db.refresh(document)
        return document

    async def retrieve(
        self, db: Session, query: str, workspace_id: str, top_k: int = 5,
        viewer_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """``viewer_id`` 决定能看到哪些私有文档；不传只检索共享文档。

        默认收紧的理由见 ``HybridRetriever.retrieve``：漏传是"少检索到自己的东西"，
        反过来是越权。
        """
        return await self.retriever.retrieve(
            db, workspace_id, query, top_k=top_k, viewer_id=viewer_id
        )

    async def search(
        self, db: Session, query: str, workspace_id: str, top_k: int = 5,
        viewer_id: str | None = None,
    ) -> list[dict]:
        """检索接口，返回可直接 JSON 序列化的结果。"""
        chunks = await self.retrieve(db, query, workspace_id, top_k, viewer_id=viewer_id)
        return [chunk.as_dict() for chunk in chunks]

    async def build_rag_context_with_citations(
        self, db: Session, query: str, workspace_id: str, top_k: int = 5,
        viewer_id: str | None = None,
    ) -> tuple[str, list[dict]]:
        """返回 (喂给模型的参考内容, 结构化引用列表)。"""
        chunks = await self.retrieve(db, query, workspace_id, top_k, viewer_id=viewer_id)
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

    async def find_document(
        self,
        db: Session,
        doc_id: str,
        workspace_id: str,
        viewer_id: str | None = None,
        *,
        include_member_private: bool = False,
    ) -> Document | None:
        """按 id 取一篇**这个人看得见的**文档。

        单独一个方法是为了让路由能在删除**之前**拿到文档做权限判断
        （``workspace_service.require_can_modify`` 需要 ``visibility`` 与 ``user_id``）。
        把权限判断塞进 ``delete_document`` 会让服务层依赖"当前用户"这个概念，
        而它现在只依赖工作区——那条边界值得保留。

        可见范围和 ``get_documents`` 共用 ``listable_documents``，两处不一致就会出现
        **能看见但删不掉**：私有文档跟人走，一个人换工作区之后旧空间里的私有文档
        仍然列得出来，而这里只按当前 ``workspace_id`` 查就找不到它，删除报 404。

        ``include_member_private=True`` 时 admin 也能取到成员的私有文档。**这不是
        给他删除权**——``require_can_modify`` 仍然会拒。让他取得到只是为了错误码
        诚实：那一篇明明在他的列表里，报 404 说"不存在"是撒谎，403 说"这是他人的
        个人文档"才是真话。
        """
        return (
            db.query(Document)
            .filter(
                Document.id == doc_id,
                workspace_service.listable_documents(
                    workspace_id,
                    viewer_id,
                    include_member_private=include_member_private,
                ),
            )
            .first()
        )

    async def delete_document(
        self, db: Session, doc_id: str, workspace_id: str, viewer_id: str | None = None
    ) -> bool:
        """删除文档及其所有分块（cascade），并让相关索引失效。

        **不做权限判断**——调用方必须先过 ``require_can_modify``。这里只保证
        "看不见的删不掉"。放在这里会让服务层需要知道当前用户是谁，见 ``find_document``。

        失效要清两处，因为私有文档跟人走：文档**自己所属**的工作区（共享文档的
        读者都在那里），以及**所有者本人**的桶（他可能正在另一个工作区里检索）。
        只清一处的症状是删了还能搜到，而这恰好是最不该留的残留。
        """
        # 刻意**不**传 include_member_private:这一层的职责是"看不见的删不掉",
        # 而删除的可见范围就该是最窄的那个。路由为了给出诚实的 403 会带上 admin
        # 的管理可见性,但那只影响它自己那次查询;真要有人绕过 require_can_modify
        # 直接调到这里,成员的私有文档在这一句就查不出来,删除返回 False。
        document = await self.find_document(db, doc_id, workspace_id, viewer_id)
        if not document:
            return False
        owner_id = document.user_id
        document_workspace = document.workspace_id or workspace_id
        db.delete(document)
        db.commit()
        for scope in {document_workspace, workspace_id}:
            invalidate_scope_indexes(scope)
            semantic_cache.invalidate_user(scope)
        if owner_id:
            # 按用户清：这个人的桶键是 ``<他当前所在工作区>|<他>``，而那个工作区
            # 未必是文档所属的那个。invalidate_by_viewer 扫的是键的后半段。
            invalidate_viewer_indexes(owner_id)
            semantic_cache.invalidate_viewer(owner_id)
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
