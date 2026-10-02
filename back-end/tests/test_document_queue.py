"""持久入库队列：enqueue/claim/complete/retry/reap/progress 的语义 + 抽干编排（替身）。

同步核心用 db_real 直接验（都收 db 参数）；抽干 run_pending 把 database.SessionLocal
绑到同一个 db_real、_run_index 换成设状态的替身，不打真实 embedding。
"""
from __future__ import annotations

from datetime import timedelta

from config import settings
from models import Document, DocumentJob
from services import document_queue
from services.clock import naive_now
from conftest import run


def test_enqueue_creates_queued_job_and_dedups(db_real):
    jid = document_queue.enqueue(db_real, "d1")
    assert jid is not None
    job = db_real.query(DocumentJob).one()
    assert job.document_id == "d1" and job.status == "queued" and job.attempts == 0
    # 已有未结束任务 → 去重，返回 None，不新增
    assert document_queue.enqueue(db_real, "d1") is None
    assert db_real.query(DocumentJob).count() == 1


def test_claim_next_marks_running_and_leases(db_real, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_QUEUE_LEASE_SECONDS", 600.0)
    document_queue.enqueue(db_real, "d1")
    claimed = document_queue.claim_next(db_real, "worker-a")
    assert claimed is not None and claimed.document_id == "d1" and claimed.attempts == 1
    job = db_real.query(DocumentJob).one()
    assert job.status == "running" and job.lease_owner == "worker-a"
    assert job.lease_expires_at is not None and job.progress == 10
    # 没有别的 queued 任务了
    assert document_queue.claim_next(db_real, "worker-a") is None


def test_claim_skips_future_available_at(db_real):
    """退避把 available_at 推到未来的任务不该被认领。"""
    document_queue.enqueue(db_real, "d1")
    job = db_real.query(DocumentJob).one()
    job.available_at = naive_now() + timedelta(seconds=120)
    db_real.commit()
    assert document_queue.claim_next(db_real, "w") is None


def test_complete_sets_succeeded(db_real):
    document_queue.enqueue(db_real, "d1")
    claimed = document_queue.claim_next(db_real, "w")
    document_queue.complete(db_real, claimed.id)
    job = db_real.query(DocumentJob).one()
    assert job.status == "succeeded" and job.progress == 100 and job.lease_owner is None


def test_fail_with_retry_requeues_then_fails_at_max(db_real, monkeypatch):
    monkeypatch.setattr(settings, "INGEST_QUEUE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(settings, "INGEST_QUEUE_BACKOFF_SECONDS", 30.0)
    document_queue.enqueue(db_real, "d1")

    # 第一次认领 + 失败 → attempts=1 < 2 → 退避重排
    c1 = document_queue.claim_next(db_real, "w")
    assert document_queue.fail_with_retry(db_real, c1.id, "boom") is True
    job = db_real.query(DocumentJob).one()
    assert job.status == "queued" and job.attempts == 1 and job.lease_owner is None
    assert job.available_at > naive_now()  # 退避推到未来
    assert job.error == "boom"

    # 把退避清掉以便再认领；第二次失败 → attempts=2 == max → failed
    job.available_at = naive_now()
    db_real.commit()
    c2 = document_queue.claim_next(db_real, "w")
    assert c2.attempts == 2
    assert document_queue.fail_with_retry(db_real, c2.id, "boom2") is False
    assert db_real.query(DocumentJob).one().status == "failed"


def test_reap_expired_leases_requeues(db_real):
    document_queue.enqueue(db_real, "d1")
    claimed = document_queue.claim_next(db_real, "dead-worker")
    # 手动把租约推到过去（模拟认领它的进程死了）
    job = db_real.get(DocumentJob, claimed.id)
    job.lease_expires_at = naive_now() - timedelta(seconds=1)
    db_real.commit()

    assert document_queue.reap_expired_leases(db_real) == 1
    job = db_real.get(DocumentJob, claimed.id)
    assert job.status == "queued" and job.lease_owner is None
    # attempts 不在回收时加（认领时已加过），所以仍是 1
    assert job.attempts == 1


def test_reap_expired_at_max_attempts_fails(db_real, monkeypatch):
    """用满尝试的任务租约过期 → 直接 failed，不再复活。"""
    monkeypatch.setattr(settings, "INGEST_QUEUE_MAX_ATTEMPTS", 1)
    document_queue.enqueue(db_real, "d1")
    claimed = document_queue.claim_next(db_real, "dead")  # attempts → 1 == max
    job = db_real.get(DocumentJob, claimed.id)
    job.lease_expires_at = naive_now() - timedelta(seconds=1)
    db_real.commit()
    assert document_queue.reap_expired_leases(db_real) == 1
    assert db_real.get(DocumentJob, claimed.id).status == "failed"


def test_set_progress_clamps_and_pending_count(db_real):
    document_queue.enqueue(db_real, "d1")
    document_queue.enqueue(db_real, "d2")
    assert document_queue.pending_count(db_real) == 2
    job = document_queue.job_for_document(db_real, "d1")
    document_queue.set_progress(db_real, job.id, 250)
    assert db_real.get(DocumentJob, job.id).progress == 100


# ========== 抽干编排（run_pending）==========


def _bind(monkeypatch, db_real):
    """把 database.SessionLocal 绑到共享的 db_real（close 变 no-op 以免关掉它）。"""
    import database

    monkeypatch.setattr(db_real, "close", lambda: None)
    monkeypatch.setattr(database, "SessionLocal", lambda: db_real)
    monkeypatch.setattr(document_queue, "_draining", False, raising=False)


def _seed_doc(db, doc_id, status="processing"):
    db.add(Document(id=doc_id, name=f"{doc_id}.md", size=1, content="正文",
                    workspace_id="w1", user_id="u1", visibility="workspace",
                    status=status, chunks=0))
    db.commit()


def test_run_pending_completes_on_indexed(db_real, monkeypatch):
    _bind(monkeypatch, db_real)
    _seed_doc(db_real, "d1")
    document_queue.enqueue(db_real, "d1")

    async def fake_index(db, document_id):
        doc = db.get(Document, document_id)
        doc.status = "indexed"
        db.commit()

    monkeypatch.setattr(document_queue, "_run_index", fake_index)
    run(document_queue.run_pending(concurrency=1))

    job = document_queue.job_for_document(db_real, "d1")
    assert job.status == "succeeded" and job.progress == 100


def test_run_pending_retries_on_failed(db_real, monkeypatch):
    _bind(monkeypatch, db_real)
    _seed_doc(db_real, "d1")
    document_queue.enqueue(db_real, "d1")

    async def fake_index(db, document_id):
        doc = db.get(Document, document_id)
        doc.status = "failed"
        db.commit()

    monkeypatch.setattr(document_queue, "_run_index", fake_index)
    run(document_queue.run_pending(concurrency=1))

    job = document_queue.job_for_document(db_real, "d1")
    # 失败且 attempts(1) < max → 退避重排，不是终态 failed
    assert job.status == "queued" and job.attempts == 1
