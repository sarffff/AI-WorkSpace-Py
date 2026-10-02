"""多 worker + memory 向量库的启动告警（main._check_multiworker_vector_store）。

memory 索引每个 worker 各建一份，多 worker 下刚传的文档会间歇检索不到。这条按
WEB_CONCURRENCY 约定给告警（不硬拦——worker 数没有可靠自省途径）。
"""
from __future__ import annotations

import main
from services import vector_store


def test_warns_on_multiworker_memory(capsys, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setattr(vector_store, "uses_qdrant", lambda: False)
    main._check_multiworker_vector_store()
    out = capsys.readouterr().out
    assert "WEB_CONCURRENCY=4" in out and "qdrant" in out.lower()


def test_silent_single_worker(capsys, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setattr(vector_store, "uses_qdrant", lambda: False)
    main._check_multiworker_vector_store()
    assert capsys.readouterr().out == ""


def test_silent_when_on_qdrant(capsys, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setattr(vector_store, "uses_qdrant", lambda: True)
    main._check_multiworker_vector_store()
    assert capsys.readouterr().out == ""


def test_silent_when_concurrency_unset(capsys, monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(vector_store, "uses_qdrant", lambda: False)
    main._check_multiworker_vector_store()
    assert capsys.readouterr().out == ""
