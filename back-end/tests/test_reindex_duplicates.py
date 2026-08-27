"""重新索引不应该留下旧分块。

**为什么会有这个文件。** 2026-08-23 往 eval 语料里加了 7 篇文档，触发
``ensure_corpus`` 重建。事后发现原有 6 篇的实际分块数正好是 ``Document.chunks``
记录值的两倍（14/7、14/7、10/5……），而新加的 7 篇是对的。

链路是这样断的：

1. ``create_document`` 按正文哈希去重，同内容重传返回 ``(existing, True)``；
2. ``upload_document`` 把那个 ``True`` 丢掉了（``_duplicate``，下划线开头即
   "我不用它"），无条件继续调 ``index_document``；
3. ``index_document`` 只 ``db.add`` 新分块，**从不删旧的**，最后把
   ``document.chunks`` 覆盖成本次的数量。

于是元数据说 7、库里有 14。``create_document`` 的文档字符串里那句"重复文档会在
检索时占据 top_k 的多个席位，把其他文档挤出去"正是发生的事——去重挡住了重复
**文档**，没挡住重复**分块**。

**影响面（核实过，比第一版判断的窄）。** HTTP 上传路径本来就是安全的：
``knowledge_router.py`` 在 ``if not duplicate`` 里才排索引任务，重复上传直接
返回已有文档。漏掉这个守卫的只有 ``upload_document``——而它的注释明说"保留给
脚本使用"。所以这是 **eval harness 的缺陷，不是线上缺陷**：
``ensure_corpus`` 在文件数变化时对全部文件重传，于是老文档被重新索引一遍。

后果：``corpusChunks`` 按 ``Document.chunks`` 求和报 92，实际 132；喂给重排的
20 条候选里 6 条是同一段内容的副本，有效候选只剩 14 条。更隐蔽的是它让
``precision@5`` **虚高**——重复占着 top_k 的位置，而它们是正确文档的副本，
去重后精度从 0.4638 掉到 0.4417，那才是真实值。

``index_document`` 本身的追加行为仍然是个隐患（任何将来新增的重索引入口都会
踩到），所以修在那一层而不是只给 ``upload_document`` 加守卫。
"""
from __future__ import annotations

import pytest
from conftest import run

from config import settings
from models import Document, DocumentChunk
from services.knowledge_service import KnowledgeService

_TEXT = """# 测试文档

## 第一节

这是第一节的内容，用来产生第一个分块。

## 第二节

这是第二节的内容，用来产生第二个分块。

## 第三节

这是第三节的内容，用来产生第三个分块。
"""


@pytest.fixture
def service(monkeypatch):
    """KnowledgeService，embedding 与自检都替成本地实现，不发网络请求。"""
    svc = KnowledgeService()

    async def fake_embed(texts):
        # 维度随便，但必须与 chunk 数一一对应——数量不匹配时 index_document
        # 会抛 RuntimeError，那是另一条路径
        return [[float(len(t) % 7), 1.0, 0.0] for t in texts]

    monkeypatch.setattr(svc.embedding, "embed_texts", fake_embed)
    # 自检要走检索，依赖进程内索引与真实 embedding；这里只关心分块行数
    monkeypatch.setattr(settings, "INGEST_SELF_CHECK", False)
    return svc


def _chunk_rows(db, document_id: str) -> list[int]:
    return [
        row[0]
        for row in db.query(DocumentChunk.chunk_index)
        .filter(DocumentChunk.document_id == document_id)
        .all()
    ]


def test_reindex_replaces_chunks_instead_of_appending(db_real, service):
    """同一文档索引两次，分块数不变，且与 Document.chunks 一致。"""
    document, duplicate = run(
        service.create_document(
            db_real, "reindex.md", _TEXT.encode("utf-8"), "ws-reindex"
        )
    )
    assert duplicate is False
    run(service.index_document(db_real, document.id))
    db_real.refresh(document)

    first = _chunk_rows(db_real, document.id)
    assert first, "第一次索引就没切出块，测试前提不成立"
    assert document.chunks == len(first)

    # 再索引一次。ensure_corpus 重建、失败重试、手工重跑都会走这条路径
    run(service.index_document(db_real, document.id))
    db_real.refresh(document)

    second = _chunk_rows(db_real, document.id)
    assert len(second) == len(first), (
        f"重新索引后分块从 {len(first)} 变成 {len(second)}——旧分块没删"
    )
    assert document.chunks == len(second), (
        f"Document.chunks={document.chunks} 与实际行数 {len(second)} 不一致"
    )


def test_chunk_indexes_stay_unique_after_reindex(db_real, service):
    """重新索引后不应出现同一个 chunk_index 的两行。

    单独立一条是因为总数相等不代表内容对：先删后加、删错一批也能凑出同样的
    总数。chunk_index 重复是"同一段进了两次"的直接证据，而 ``read_chunks``
    按 (document_id, chunk_index) 取邻域，重复会让邻域扩展拿到两份同样的文字。
    """
    document, _ = run(
        service.create_document(
            db_real, "reindex-idx.md", _TEXT.encode("utf-8"), "ws-reindex-idx"
        )
    )
    run(service.index_document(db_real, document.id))
    run(service.index_document(db_real, document.id))

    indexes = _chunk_rows(db_real, document.id)
    assert len(indexes) == len(set(indexes)), f"chunk_index 出现重复: {sorted(indexes)}"


def test_upload_same_content_twice_does_not_double_chunks(db_real, service):
    """走 upload_document 的完整路径：同内容重传两次，分块不翻倍。

    这条最接近真实触发方式——``ensure_corpus`` 就是对每个文件调
    ``upload_document``，而它内部的去重会把第二次解析成同一行文档。
    """
    payload = _TEXT.encode("utf-8")
    first = run(service.upload_document(db_real, "dup.md", payload, "ws-dup"))
    first_rows = _chunk_rows(db_real, first.id)

    second = run(service.upload_document(db_real, "dup.md", payload, "ws-dup"))
    assert second.id == first.id, "按内容哈希去重应当返回同一行文档"

    second_rows = _chunk_rows(db_real, first.id)
    assert len(second_rows) == len(first_rows), (
        f"同内容重传后分块从 {len(first_rows)} 变成 {len(second_rows)}"
    )
    assert (
        db_real.query(Document).filter(Document.workspace_id == "ws-dup").count() == 1
    )
