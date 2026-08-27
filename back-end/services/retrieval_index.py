"""检索索引：稠密向量 + BM25 稀疏检索，以及两者的 RRF 融合。

单路稠密检索的盲区很具体：专有名词、错别字、精确的 API 名或编号，向量化之后
语义相近但字面不同的东西会被拉到一起，而"字面完全命中"反而排不上去。BM25 恰好
补上这一块。两路分数量纲不同（余弦相似度 vs TF-IDF 加权和），直接加权相加需要
先做分数归一化且对分布敏感，所以这里用 RRF——只看排名不看分数。

索引仍然是进程内、按用户隔离、按签名失效。多 worker 部署时每个 worker 各建一份，
这是把向量搬到 pgvector / Qdrant 之前的已知限制。
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections import Counter
from typing import Any, Callable, Optional

import numpy as np

from config import settings
from models import DocumentChunk
from services.embedding_service import EmbeddingService

logger = logging.getLogger("retrieval_index")

try:
    import faiss  # type: ignore

    _FAISS_AVAILABLE = True
except ImportError:
    _FAISS_AVAILABLE = False
    logger.info("faiss not installed, falling back to numpy cosine similarity.")


_LATIN_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]+")


def tokenize(text: str) -> list[str]:
    """BM25 用的分词。

    中文没有空格，按词切需要词典（jieba 之类）。这里用字符 bigram 作为无依赖的
    替代：召回稍宽，但短查询下很稳，也不会因为分词器把专有名词切错而漏召回。
    拉丁字母与数字按小写词元处理。
    """
    if not text:
        return []
    tokens = _LATIN_TOKEN_RE.findall(text.lower())
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def signature_from_ids(chunk_ids: list[str]) -> str:
    """索引签名:对 chunk id 集合(按稳定顺序)做哈希。

    以前是对全部 embedding/content 字符串做 sha256,每次检索都要把整个库的
    大 Text 字段拉进内存再哈希一遍——检索成本随库规模线性增长,而这发生在
    每一条查询上。换成 id 集合签名后,判失效只需一次只查 id 列的轻量查询。

    正确性前提:chunk 行只增删、从不原地更新(入库时整体插入,删文档时
    级联删除)。任何 id 集合的变化都会改变签名;若将来引入原地 re-embed,
    必须换回内容签名或引入显式版本号。
    """
    digest = hashlib.sha256()
    for chunk_id in chunk_ids:
        digest.update(chunk_id.encode("utf-8"))
    return digest.hexdigest()


class VectorIndex:
    """进程内向量索引：按签名失效，FAISS 可用时走 IndexFlatIP，否则 numpy。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: list[str] = []
        self._matrix: Optional[np.ndarray] = None  # (n, d) float32, 已 L2 归一化
        self._faiss_index = None
        # _built 区分"从没建过"和"建过但向量集为空"(后者 _matrix 就是 None),
        # 否则空索引每次检索都会白白重建一遍
        self._built = False
        self._signature = ""
        self._dimension = 0

    def is_fresh(self, signature: str) -> bool:
        with self._lock:
            return self._built and signature == self._signature

    def build_if_stale(self, chunks: list[DocumentChunk], signature: str) -> None:
        with self._lock:
            if self._built and signature == self._signature:
                return

            ids: list[str] = []
            vectors: list[list[float]] = []
            dimension = 0
            for chunk in chunks:
                if not chunk.embedding:
                    continue
                try:
                    embedding_model = EmbeddingService.deserialize_model(chunk.embedding)
                    vector = EmbeddingService.deserialize(chunk.embedding)
                except (TypeError, ValueError):
                    logger.warning("Skipping invalid embedding for chunk %s", chunk.id)
                    continue
                if embedding_model and embedding_model != settings.EMBEDDING_MODEL:
                    logger.warning(
                        "Skipping chunk %s from embedding model %s (current: %s)",
                        chunk.id,
                        embedding_model,
                        settings.EMBEDDING_MODEL,
                    )
                    continue
                if not vector:
                    continue
                if dimension == 0:
                    dimension = len(vector)
                if len(vector) != dimension:
                    logger.warning(
                        "Skipping chunk %s because embedding dimension %s != %s",
                        chunk.id,
                        len(vector),
                        dimension,
                    )
                    continue
                vectors.append(vector)
                ids.append(chunk.id)

            self._built = True
            self._signature = signature
            self._ids = ids
            self._dimension = dimension
            if not vectors:
                self._matrix = None
                self._faiss_index = None
                self._dimension = 0
                return

            matrix = np.asarray(vectors, dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._matrix = matrix / norms  # 归一化后内积即余弦相似度

            if _FAISS_AVAILABLE:
                self._faiss_index = self._build_faiss(self._matrix)
            else:
                self._faiss_index = None

    @staticmethod
    def _build_faiss(matrix: "np.ndarray"):
        """按 ``VECTOR_ANN`` 建精确索引或 HNSW 图。

        HNSW 在这个语料规模下**只会更差**：几千个向量的暴力内积本来就是毫秒级，
        而 HNSW 既有建图开销又有召回损失。它存在的意义是让"ANN 要付多少代价"变成
        一个能量出来的数（``recall@5`` 与 ``avgRetrievalMs`` 一起看），而不是一句
        "到了大规模就该上 ANN"的口号——真到那个规模时，代价曲线长什么样得先知道。

        用 ``IndexHNSWFlat`` + ``METRIC_INNER_PRODUCT``：向量已经 L2 归一化，
        所以内积就是余弦相似度，和 ``IndexFlatIP`` 那条路的分数量纲一致。量纲不一致
        会让 ``RAG_MIN_SCORE`` 这个阈值在两个后端下含义不同——那种 bug 不会报错。
        """
        dimension = matrix.shape[1]
        if (settings.VECTOR_ANN or "").strip().lower() != "hnsw":
            index = faiss.IndexFlatIP(dimension)
            index.add(matrix)
            return index

        index = faiss.IndexHNSWFlat(
            dimension, max(4, settings.VECTOR_HNSW_M), faiss.METRIC_INNER_PRODUCT
        )
        index.hnsw.efConstruction = max(8, settings.VECTOR_HNSW_EF_CONSTRUCT)
        index.hnsw.efSearch = max(8, settings.VECTOR_HNSW_EF_SEARCH)
        index.add(matrix)
        logger.info(
            "built HNSW index: n=%s d=%s M=%s efC=%s efS=%s",
            matrix.shape[0],
            dimension,
            settings.VECTOR_HNSW_M,
            settings.VECTOR_HNSW_EF_CONSTRUCT,
            settings.VECTOR_HNSW_EF_SEARCH,
        )
        return index

    def search(self, query_vector: list[float], top_k: int = 5) -> list[tuple[float, str]]:
        """返回 [(余弦相似度, chunk_id), ...]，按分数降序。"""
        with self._lock:
            if self._matrix is None or not self._ids:
                return []
            if len(query_vector) != self._dimension:
                logger.error(
                    "Query embedding dimension %s does not match index dimension %s",
                    len(query_vector),
                    self._dimension,
                )
                return []

            query = np.asarray([query_vector], dtype=np.float32)
            norm = np.linalg.norm(query, axis=1, keepdims=True)
            norm[norm == 0] = 1.0
            query = query / norm

            limit = min(top_k, len(self._ids))
            if _FAISS_AVAILABLE and self._faiss_index is not None:
                scores, indices = self._faiss_index.search(query, limit)
                return [
                    (float(score), self._ids[index])
                    for score, index in zip(scores[0], indices[0])
                    if index >= 0
                ]

            similarities = (self._matrix @ query.T).reshape(-1)
            top_indices = np.argsort(-similarities)[:limit]
            return [(float(similarities[i]), self._ids[i]) for i in top_indices]


class BM25Index:
    """进程内 BM25 稀疏索引。

    实现的是 BM25Okapi：词频饱和（k1）+ 文档长度归一化（b）+ 逆文档频率。
    比朴素 TF-IDF 的关键改进是词频饱和——一个词在长文档里重复 50 次，
    并不意味着相关度是重复 5 次的 10 倍。
    """

    K1 = 1.5
    B = 0.75

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._built = False
        self._signature = ""
        self._ids: list[str] = []
        self._term_freqs: list[Counter[str]] = []
        self._lengths: list[int] = []
        self._doc_freq: Counter[str] = Counter()
        self._average_length = 0.0

    def is_fresh(self, signature: str) -> bool:
        with self._lock:
            return self._built and signature == self._signature

    def build_if_stale(self, chunks: list[DocumentChunk], signature: str) -> None:
        with self._lock:
            if self._built and signature == self._signature:
                return

            self._built = True
            self._signature = signature
            self._ids = []
            self._term_freqs = []
            self._lengths = []
            self._doc_freq = Counter()

            for chunk in chunks:
                tokens = tokenize(chunk.content or "")
                if not tokens:
                    continue
                frequencies = Counter(tokens)
                self._ids.append(chunk.id)
                self._term_freqs.append(frequencies)
                self._lengths.append(len(tokens))
                self._doc_freq.update(frequencies.keys())

            self._average_length = (
                sum(self._lengths) / len(self._lengths) if self._lengths else 0.0
            )

    def search(self, query: str, top_k: int = 5) -> list[tuple[float, str]]:
        """返回 [(BM25 分数, chunk_id), ...]，按分数降序，零分不返回。"""
        query_terms = tokenize(query)
        if not query_terms:
            return []

        with self._lock:
            if not self._ids:
                return []
            total_docs = len(self._ids)
            average_length = self._average_length or 1.0

            idf: dict[str, float] = {}
            for term in set(query_terms):
                doc_freq = self._doc_freq.get(term, 0)
                if doc_freq == 0:
                    continue
                # 加 1 保证 idf 恒为正，避免高频词产生负分把文档推到末尾
                idf[term] = math.log(
                    1 + (total_docs - doc_freq + 0.5) / (doc_freq + 0.5)
                )
            if not idf:
                return []

            scored: list[tuple[float, str]] = []
            for position, chunk_id in enumerate(self._ids):
                frequencies = self._term_freqs[position]
                length = self._lengths[position]
                score = 0.0
                for term, term_idf in idf.items():
                    frequency = frequencies.get(term, 0)
                    if not frequency:
                        continue
                    denominator = frequency + self.K1 * (
                        1 - self.B + self.B * length / average_length
                    )
                    score += term_idf * frequency * (self.K1 + 1) / denominator
                if score > 0:
                    scored.append((score, chunk_id))

            scored.sort(key=lambda item: (-item[0], item[1]))
            return scored[:top_k]


def reciprocal_rank_fusion(
    rankings: list[list[str]],
    k: int = 60,
    weights: list[float] | None = None,
    tiebreak: Callable[[str], Any] | None = None,
) -> list[tuple[str, float]]:
    """RRF 融合多条召回通道。

    每条通道贡献 ``weight / (k + rank)``，只用排名不用分数——这样余弦相似度和 BM25
    分数不需要归一化就能合并，也不会因为某一路分数尺度大而独占结果。
    常数 k（经验值 60）压低头部名次的权重差，让多路都命中的文档更容易冒头。

    ``weights`` 让不同通道有不同话语权：一个精确编号查询本该偏 BM25，一个
    改述式提问本该偏稠密向量，而"两路各占一半"对两者都不是最优。缺省 ``None``
    等价于全 1.0，与加权之前**逐位相同**——这一点是有意保证的，否则所有既有
    评估基线都会因为一次重构而失效，没法再和新数字比。

    权重个数与通道数不匹配时按 1.0 补齐而不是抛错：多查询改写会让通道数变成
    动态的（每个改写变体各贡献两路），调用方很难预先算准个数。

    **``tiebreak`` 不是可选的讲究，是修一个真实的不确定性。** RRF 的并列是
    *结构性*的：两个块只要在 dense 与 sparse 里相邻互换位置，两边各拿一个第 n
    一个第 n+1，融合分就是同一个 ``w/(k+n) + w/(k+n+1)``——不是"接近"，是浮点
    位位相同。这种情况很常见，而默认按 ``chunk_id`` 打破并列意味着胜者由**随机
    UUID** 决定：同一份语料重新索引一次，top-1 就可能换人。

    2026-08-23 量到的实例："什么职级可以买高铁一等座？" 里 ``travel-booking``
    在 dense 第 1、sparse 第 2，``expense-policy`` 恰好相反，两者融合分都是
    ``1/61 + 1/62 = 0.03252247488101534``。重新索引前后 top-1 互换，期望文档
    从第 1 位掉到第 2 位，那条题的 nDCG 在 1.0 与 0.6309 之间跳——**改动无关的
    语料也会让别的用例变色**，评估里读到的"提升"可能只是换了一批 UUID。

    传入按"文档名 + 块序号"这类**跨重建稳定**的键，同一份语料就永远给出同一个
    顺序。不传时退回 ``chunk_id``，保持既有调用方与基线数字不变。
    """
    scores: dict[str, float] = {}
    for index, ranking in enumerate(rankings):
        weight = 1.0
        if weights is not None and index < len(weights):
            weight = weights[index]
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank)
    key = tiebreak or (lambda chunk_id: chunk_id)
    return sorted(scores.items(), key=lambda item: (-item[1], key(item[0])))


class _UserIndexes:
    __slots__ = ("vector", "bm25")

    def __init__(self) -> None:
        self.vector = VectorIndex()
        self.bm25 = BM25Index()


# 桶键是"检索作用域"。加可见性之前它就是 workspace_id;现在因为"能检索到什么"
# 因人而异(共享文档 + 自己的私有文档),它是 workspace_id 或 workspace_id|viewer_id,
# 由 scope_key() 拼。
_indexes: dict[str, _UserIndexes] = {}
_indexes_lock = threading.Lock()

_SCOPE_SEPARATOR = "|"


def scope_key(workspace_id: str, viewer_id: str | None = None) -> str:
    """拼检索作用域键。

    ``viewer_id`` 为 None 时退回纯工作区键——那是"只检索共享文档"的语义,
    离线评估与脚本走这条路。

    **为什么按人分桶。** 索引是按 SQL 查回来的行建的,而那批行现在取决于查看者
    (共享文档 + 他自己的私有文档)。共用一份索引就意味着别人的私有分块也在 BM25
    词表里——正确性上仍然安全(``_retrieve`` 里 ``chunk_id in by_id`` 会把它们滤掉),
    但那是把隔离押在一个后置过滤上,而且会挤占 per-channel 的候选席位。

    **代价说清楚。** 每个活跃用户一份索引,共享文档在每份里各占一次内存。
    不是重新 embedding(``build_if_stale`` 从库里已存的 embedding 建索引,不发请求),
    但内存确实随活跃用户数线性增长。
    可接受的前提是"一个工作区几个人";上百人的工作区应当改成
    "共享索引一份 + 每人一份小私有索引,检索时各取 top-k 再 RRF 融合"——
    RRF 已经在 ``reciprocal_rank_fusion`` 里,那条路不需要新机制,只需要
    ``_retrieve`` 支持两个来源。留到真有那个规模再做。
    """
    if not viewer_id:
        return workspace_id
    return f"{workspace_id}{_SCOPE_SEPARATOR}{viewer_id}"


def get_scope_indexes(scope_id: str) -> _UserIndexes:
    """按检索作用域隔离索引，避免跨工作区/跨用户的结果混合。"""
    with _indexes_lock:
        return _indexes.setdefault(scope_id, _UserIndexes())


def indexes_fresh(scope_id: str, signature: str) -> bool:
    """该作用域的索引是否已经按这个签名构建完成。

    检索方先做一次只查 id 列的轻量查询算签名,再调这里:新鲜就能跳过
    embedding 大字段的拉取与重建。BM25 未启用时不参与判断。
    """
    with _indexes_lock:
        bundle = _indexes.get(scope_id)
    if bundle is None:
        return False
    if not bundle.vector.is_fresh(signature):
        return False
    if settings.RAG_HYBRID and not bundle.bm25.is_fresh(signature):
        return False
    return True


def invalidate_scope_indexes(scope_id: str) -> None:
    """文档增删后调用；下次检索时重建。``scope_id`` 传工作区 id。

    **按前缀清掉该工作区下所有分桶**,不只是同名那一个。加可见性之后一个工作区
    有多个桶(``ws`` 与 ``ws|viewer``),而共享文档的增删影响其中每一个——
    只 pop 精确键会让所有带 viewer 的桶继续用旧索引,症状是"admin 传了新文档,
    别人搜不到,重启才好"。

    传一个具体的 ``ws|viewer`` 键也是合法的(只清那一个人的桶),那是私有文档
    增删的场景。前缀匹配对它同样成立且更宽,清多了只是多一次重建。
    """
    prefix = f"{scope_id}{_SCOPE_SEPARATOR}"
    with _indexes_lock:
        for key in [
            key
            for key in _indexes
            if key == scope_id or key.startswith(prefix)
        ]:
            _indexes.pop(key, None)


def invalidate_viewer_indexes(viewer_id: str) -> None:
    """清掉某个人的所有桶,**不论他在哪个工作区**。

    存在的理由是私有文档跟人走(见 ``HybridRetriever._retrievable_by``):一个人的
    私有文档增删要影响的是"他的"索引,而他的桶键是
    ``<他当前所在工作区>|<他>``——那个工作区未必等于文档的 ``workspace_id``
    (文档可能是他在上一个空间里传的)。所以按工作区前缀清是清不到的,
    这里改成扫键的后半段。

    症状如果漏掉:删了自己的私有文档,当前会话里还能搜到它。
    """
    suffix = f"{_SCOPE_SEPARATOR}{viewer_id}"
    with _indexes_lock:
        for key in [key for key in _indexes if key.endswith(suffix)]:
            _indexes.pop(key, None)
