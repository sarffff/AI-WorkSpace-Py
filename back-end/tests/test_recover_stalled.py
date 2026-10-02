"""重启后恢复卡在 ``processing`` 的文档（main._recover_stalled_documents）。

索引现在走持久的 document_jobs 队列，但进程在索引中途被杀仍可能留下文档停在
``processing``。这个启动钩子把它们重新**入队**（而不是改动前的进程内 create_task），
重启不丢、失败重试。

测的是"哪些被重新入队"：只有 ``processing`` 的该重排，``indexed`` / ``failed`` 是
终态——重排 indexed 是浪费、重排 failed 会把一个已如实报告失败的文档又变回处理中。
"""
from __future__ import annotations

import asyncio

from conftest import run
from models import Document, DocumentJob


def _seed(db, doc_id: str, status: str) -> None:
    db.add(
        Document(
            id=doc_id,
            name=f"{doc_id}.md",
            size=1,
            content="正文",
            workspace_id="w1",
            user_id="u1",
            visibility="workspace",
            status=status,
            chunks=0,
        )
    )
    db.commit()


def _patch(monkeypatch, db_real) -> None:
    """SessionLocal 指到测试库；抽干换成 no-op（否则会打真实 embedding）。"""
    import main
    from services import document_queue

    async def noop(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(document_queue, "run_pending", noop)
    monkeypatch.setattr(main, "SessionLocal", lambda: db_real)


async def _drive(main) -> None:
    # _recover_stalled_documents 入队后用 create_task 触发抽干，需要一个在跑的 loop；
    # 抽干已被 no-op 掉，sleep(0) 让那个 task 跑完即可。
    main._recover_stalled_documents()
    for _ in range(3):
        await asyncio.sleep(0)


def _job_doc_ids(db) -> list[str]:
    return sorted(job.document_id for job in db.query(DocumentJob).all())


def test_recover_enqueues_only_processing(db_real, monkeypatch):
    import main

    _seed(db_real, "d-proc", "processing")
    _seed(db_real, "d-proc2", "processing")
    _seed(db_real, "d-idx", "indexed")
    _seed(db_real, "d-fail", "failed")
    _patch(monkeypatch, db_real)

    run(_drive(main))

    assert _job_doc_ids(db_real) == ["d-proc", "d-proc2"]
    assert all(job.status == "queued" for job in db_real.query(DocumentJob).all())


def test_recover_noop_when_nothing_stalled(db_real, monkeypatch):
    """没有中断文档时不入任何队、也不报错（正常启动走的就是这条）。"""
    import main

    _seed(db_real, "d-idx", "indexed")
    _patch(monkeypatch, db_real)

    run(_drive(main))

    assert _job_doc_ids(db_real) == []
