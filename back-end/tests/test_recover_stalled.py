"""重启后恢复卡在 ``processing`` 的文档（main._recover_stalled_documents）。

上传走进程内 BackgroundTasks，不落磁盘。进程在索引中途重启，那些文档会永远停在
``processing``——既检索不到，也看不出出了问题。这个启动钩子把它们捞出来重新驱动。

测的是"哪些被重新排了"：只有 ``processing`` 的该重跑，``indexed`` / ``failed`` 是
终态，重跑 indexed 是浪费、重跑 failed 会把一个已经如实报告失败的文档又变回
processing（掩盖失败）。
"""
from __future__ import annotations

import asyncio

from conftest import run
from models import Document


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


def _patch(monkeypatch, db_real):
    """把 SessionLocal 指到测试库，把后台索引任务换成记录器。返回 scheduled 列表。"""
    import main
    from routers import knowledge_router

    scheduled: list[str] = []

    async def fake_task(doc_id: str) -> None:
        scheduled.append(doc_id)

    monkeypatch.setattr(knowledge_router, "_index_document_task", fake_task)
    monkeypatch.setattr(main, "SessionLocal", lambda: db_real)
    return scheduled


async def _drive(main):
    # _recover_stalled_documents 用 asyncio.create_task 排任务，需要一个在跑的 loop；
    # 排完 sleep(0) 几次让那些任务真的执行到（fake_task 无 await，一次让出即可跑完）。
    main._recover_stalled_documents()
    for _ in range(3):
        await asyncio.sleep(0)


def test_recover_reindexes_only_processing(db_real, monkeypatch):
    import main

    _seed(db_real, "d-proc", "processing")
    _seed(db_real, "d-proc2", "processing")
    _seed(db_real, "d-idx", "indexed")
    _seed(db_real, "d-fail", "failed")
    scheduled = _patch(monkeypatch, db_real)

    run(_drive(main))

    assert sorted(scheduled) == ["d-proc", "d-proc2"]


def test_recover_noop_when_nothing_stalled(db_real, monkeypatch):
    """没有中断文档时不排任何任务、也不报错（正常启动走的就是这条）。"""
    import main

    _seed(db_real, "d-idx", "indexed")
    scheduled = _patch(monkeypatch, db_real)

    run(_drive(main))

    assert scheduled == []
