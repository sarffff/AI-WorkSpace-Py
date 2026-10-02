"""入库索引任务的持久化队列。

改动前上传后的索引是 FastAPI ``BackgroundTasks``：同事件循环、不落盘、无并发上限、
无进度、无重试，进程在索引中途重启那篇文档就永远卡在 ``processing``。这里把"要索引
谁"落成 ``document_jobs`` 行，配认领/租约/重试，形态与 ``agent_runs`` 的 lease/reaper
完全一致——都挂在读路径上惰性清理，项目里没有调度器进程。

至少一次投递是安全的：``index_document`` 幂等（先删旧分块再写新的）。``attempts`` 在
**认领时**自增，所以租约过期被回收的任务也消耗一次尝试——一个每次都把进程拖垮的
任务不会被无限重试。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from config import settings
from models import Document, DocumentJob
from services.clock import naive_now

logger = logging.getLogger("document_queue")

_ACTIVE = ("queued", "running")


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: str
    document_id: str
    attempts: int


def enqueue(db: Session, document_id: str) -> str | None:
    """给一篇文档排一个索引任务。已有未结束任务（queued/running）就跳过去重，返回 None。

    去重是必须的：重复上传已在 create_document 层挡掉，但重试/重启重驱可能对同一篇
    再次入队，没有去重会让一篇文档被并发索引两次（彼此删对方的分块）。
    """
    try:
        existing = (
            db.query(DocumentJob.id)
            .filter(
                DocumentJob.document_id == document_id,
                DocumentJob.status.in_(_ACTIVE),
            )
            .first()
        )
        if existing is not None:
            return None
        now = naive_now()
        job = DocumentJob(
            id=str(uuid.uuid4()),
            document_id=document_id,
            status="queued",
            attempts=0,
            max_attempts=max(1, settings.INGEST_QUEUE_MAX_ATTEMPTS),
            progress=0,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.commit()
        return job.id
    except Exception:
        db.rollback()
        logger.exception("enqueue failed for document %s", document_id)
        return None


def claim_next(db: Session, worker_id: str, lease_seconds: float | None = None) -> ClaimedJob | None:
    """原子认领最老的一个可认领任务（queued 且 available_at<=now）。没有则 None。

    真实库上用 ``SELECT ... FOR UPDATE SKIP LOCKED`` 让多 worker 不会认领到同一行；
    SQLite 忽略该子句（测试里单线程，无妨）。认领即把 status 推成 running、占住租约、
    attempts 自增、进度置 10。
    """
    lease = lease_seconds if lease_seconds is not None else settings.INGEST_QUEUE_LEASE_SECONDS
    now = naive_now()
    try:
        query = (
            db.query(DocumentJob)
            .filter(DocumentJob.status == "queued", DocumentJob.available_at <= now)
            .order_by(DocumentJob.available_at.asc())
        )
        try:
            job = query.with_for_update(skip_locked=True).first()
        except Exception:
            # 方言不支持 FOR UPDATE（SQLite）——退回普通取
            job = query.first()
        if job is None:
            return None
        job.status = "running"
        job.lease_owner = worker_id[:64]
        job.lease_expires_at = now + timedelta(seconds=lease)
        job.attempts += 1
        job.progress = 10
        job.updated_at = now
        db.commit()
        return ClaimedJob(id=job.id, document_id=job.document_id, attempts=job.attempts)
    except Exception:
        db.rollback()
        logger.exception("claim_next failed for worker %s", worker_id)
        return None


def complete(db: Session, job_id: str) -> None:
    """任务成功：status=succeeded、进度 100、放掉租约。"""
    _finish(db, job_id, status="succeeded", progress=100)


def fail_with_retry(db: Session, job_id: str, error: str = "") -> bool:
    """任务失败。未到尝试上限则退避重排（返回 True），否则落 failed（返回 False）。

    退避把 ``available_at`` 推到 ``now + 基数 × attempts``——同一篇反复失败时间隔越拉
    越长，避免 embedding 端点挂了之后变成一个紧的重试循环。
    """
    try:
        job = db.get(DocumentJob, job_id)
        if job is None:
            return False
        now = naive_now()
        job.error = (error or "")[:200] or None
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        if job.attempts < job.max_attempts:
            backoff = settings.INGEST_QUEUE_BACKOFF_SECONDS * job.attempts
            job.status = "queued"
            job.available_at = now + timedelta(seconds=backoff)
            job.progress = 0
            db.commit()
            return True
        job.status = "failed"
        db.commit()
        return False
    except Exception:
        db.rollback()
        logger.exception("fail_with_retry failed for job %s", job_id)
        return False


def _finish(db: Session, job_id: str, *, status: str, progress: int) -> None:
    try:
        job = db.get(DocumentJob, job_id)
        if job is None:
            return
        now = naive_now()
        job.status = status
        job.progress = progress
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("finish(%s) failed for job %s", status, job_id)


def reap_expired_leases(db: Session, limit: int = 100) -> int:
    """租约过期的 running 任务（认领它的进程死了）重新入队。返回条数。

    attempts 不在这里加——它在认领时就加过了，所以一个每次都把进程拖垮的任务，
    被回收几次就会撞到 max_attempts 落 failed，而不是无限复活。惰性挂在知识库读
    路径与启动（同 expire_stale_runs 的做法）。
    """
    now = naive_now()
    try:
        stale = (
            db.query(DocumentJob)
            .filter(
                DocumentJob.status == "running",
                DocumentJob.lease_expires_at.isnot(None),
                DocumentJob.lease_expires_at < now,
            )
            .limit(limit)
            .all()
        )
        for job in stale:
            if job.attempts >= job.max_attempts:
                # 已经用满尝试：别再复活，直接落 failed
                job.status = "failed"
                job.error = (job.error or "lease_expired")[:200]
            else:
                job.status = "queued"
                job.available_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
        if stale:
            db.commit()
        return len(stale)
    except Exception:
        db.rollback()
        logger.exception("reap_expired_leases failed")
        return 0


def set_progress(db: Session, job_id: str, progress: int) -> None:
    try:
        job = db.get(DocumentJob, job_id)
        if job is None:
            return
        job.progress = max(0, min(100, progress))
        job.updated_at = naive_now()
        db.commit()
    except Exception:
        db.rollback()


def job_for_document(db: Session, document_id: str) -> DocumentJob | None:
    """某文档最近的任务（给 GET /knowledge/documents 带进度/尝试次数用）。"""
    return (
        db.query(DocumentJob)
        .filter(DocumentJob.document_id == document_id)
        .order_by(DocumentJob.created_at.desc())
        .first()
    )


def pending_count(db: Session) -> int:
    return db.query(DocumentJob).filter(DocumentJob.status.in_(_ACTIVE)).count()


# ---- 进程侧的抽干器（无调度器：由 enqueue 后的 BackgroundTasks 与启动重驱触发）----

_draining = False


async def _run_index(db: Session, document_id: str) -> None:
    """跑一篇的索引。单独抽出来，测试可 monkeypatch 掉以免打真实 embedding。"""
    from services.knowledge_service import KnowledgeService

    await KnowledgeService().index_document(db, document_id)


async def _process_one(claimed: ClaimedJob) -> None:
    """认领到的一篇：置 processing → 索引 → 据 Document 终态 complete 或退避重试。

    成败判据直接读 ``Document.status``（index_document 自己会落 indexed/failed），
    这样重试语义是免费的、index_document 一行不用改。
    """
    from database import SessionLocal

    db = SessionLocal()
    try:
        doc = db.get(Document, claimed.document_id)
        if doc is None:
            complete(db, claimed.id)  # 文档已删，任务无事可做
            return
        if doc.status != "processing":
            doc.status = "processing"  # 重试可见性：从 failed/其它置回 processing
            db.commit()
        set_progress(db, claimed.id, 40)
        await _run_index(db, claimed.document_id)
        db.expire_all()
        final = db.get(Document, claimed.document_id)
        if final is not None and final.status == "indexed":
            complete(db, claimed.id)
        else:
            reason = (final.parse_warnings if final and final.parse_warnings else "index_failed")
            will_retry = fail_with_retry(db, claimed.id, reason)
            logger.warning(
                "index job %s doc %s not indexed (will_retry=%s)",
                claimed.id, claimed.document_id, will_retry,
            )
    except Exception as exc:  # noqa: BLE001
        fail_with_retry(db, claimed.id, f"{type(exc).__name__}: {exc}")
        logger.exception("process job %s failed", claimed.id)
    finally:
        db.close()


async def _worker(worker_id: str) -> None:
    from database import SessionLocal

    while True:
        db = SessionLocal()
        try:
            claimed = claim_next(db, worker_id)
        finally:
            db.close()
        if claimed is None:
            return
        await _process_one(claimed)


async def run_pending(concurrency: int | None = None) -> None:
    """把队列里能认领的任务抽干。worker 协程并发，受 INGEST_QUEUE_CONCURRENCY 限。

    进程内防叠加：已经有一拨 worker 在抽时直接返回——它们会把新入队的也一并带走。
    多 worker 部署时每个进程各跑一拨，靠 claim_next 的 FOR UPDATE SKIP LOCKED 不撞车。
    """
    global _draining
    if _draining:
        return
    _draining = True
    count = concurrency or max(1, settings.INGEST_QUEUE_CONCURRENCY)
    try:
        await asyncio.gather(*[_worker(f"{id(object())}-{i}") for i in range(count)])
    finally:
        _draining = False


