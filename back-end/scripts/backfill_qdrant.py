"""把 MySQL 里已有的向量一次性回填到 Qdrant。

    python scripts/backfill_qdrant.py            # 只补缺的
    python scripts/backfill_qdrant.py --force    # 全部重写

**为什么需要这一步而不是让它自己长起来**：``VECTOR_STORE=memory`` 时向量是派生态，
每次检索按签名从 MySQL 重建；切到 qdrant 之后它是持久态，而 Qdrant 里此刻是空的。
检索侧没有办法自己补——它不知道"哪些点本该在里面"，只会得到一个空的稠密通道，
表现是「混合检索突然只剩 BM25 那一路」，召回掉一大截而且不报任何错。

按工作区分批而不是一把梭：``upsert`` 的批量大小直接决定请求体积，一个几万块的
知识库一次提交会撞 Qdrant 的请求上限。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings  # noqa: E402
from database import SessionLocal  # noqa: E402
from models import Document, DocumentChunk  # noqa: E402
from services import vector_store  # noqa: E402
from services.embedding_service import EmbeddingService  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_qdrant")

BATCH = 256


async def backfill(force: bool) -> int:
    if (settings.VECTOR_STORE or "").lower() != "qdrant":
        raise SystemExit(
            "VECTOR_STORE 不是 qdrant。先在 .env 里改掉再跑这个脚本——"
            "否则回填完的数据没有任何东西会去读它。"
        )
    store = vector_store.get_store()
    if not vector_store.uses_qdrant():
        raise SystemExit(
            f"连不上 Qdrant（{settings.QDRANT_URL}）。"
            "先 docker compose -f docker-compose.qdrant.yml up -d。"
        )

    session = SessionLocal()
    total = 0
    try:
        scopes = [
            row[0]
            for row in session.query(Document.workspace_id)
            .filter(Document.status == "indexed", Document.workspace_id.isnot(None))
            .distinct()
            .all()
        ]
        logger.info("发现 %s 个工作区", len(scopes))

        for scope_id in scopes:
            expected = (
                session.query(DocumentChunk)
                .join(Document, DocumentChunk.document_id == Document.id)
                .filter(
                    Document.workspace_id == scope_id, Document.status == "indexed"
                )
                .count()
            )
            present = await store.count(scope_id)
            if not force and present == expected:
                logger.info("[%s] 已一致（%s 块），跳过", scope_id, expected)
                continue
            logger.info(
                "[%s] Qdrant %s / MySQL %s，开始回填", scope_id, present, expected
            )

            records: list[vector_store.VectorRecord] = []
            written = 0
            query = (
                session.query(DocumentChunk, Document.id)
                .join(Document, DocumentChunk.document_id == Document.id)
                .filter(
                    Document.workspace_id == scope_id, Document.status == "indexed"
                )
                .yield_per(BATCH)
            )
            for chunk, document_id in query:
                try:
                    vector = EmbeddingService.deserialize(chunk.embedding or "")
                except (TypeError, ValueError):
                    logger.warning("跳过无法解析的向量 %s", chunk.id)
                    continue
                if not vector:
                    continue
                records.append(
                    vector_store.VectorRecord(
                        chunk_id=chunk.id, document_id=document_id, vector=vector
                    )
                )
                if len(records) >= BATCH:
                    await store.upsert(scope_id, records)
                    written += len(records)
                    records = []
            if records:
                await store.upsert(scope_id, records)
                written += len(records)
            logger.info("[%s] 写入 %s 块", scope_id, written)
            total += written
    finally:
        session.close()
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 Qdrant 向量")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使点数已经一致也全部重写（换了 embedding 模型后用）",
    )
    args = parser.parse_args()
    written = asyncio.run(backfill(args.force))
    logger.info("完成，共写入 %s 块", written)


if __name__ == "__main__":
    main()
