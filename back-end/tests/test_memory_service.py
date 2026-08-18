"""长期记忆:抽取、归一化去重、上限修剪与注入块格式。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import settings
from database import Base
from models import UserMemory
from services.clock import naive_now
from services.memory_service import MemoryService, _normalize, _parse_items
from services.model_adapter import ModelCompletion
from conftest import run

USER = "u-memory"


class StubAdapter:
    """complete() 恒定返回预设内容,记录收到的 prompt 供断言。"""

    def __init__(self, content: str):
        self._content = content
        self.calls: list[str] = []

    async def complete(self, *, messages, tools, model, **kwargs):
        self.calls.append(messages[-1]["content"])
        return ModelCompletion(content=self._content, tool_calls=[])


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[UserMemory.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _memories(db, count: int) -> None:
    for i in range(count):
        db.add(
            UserMemory(
                user_id=USER,
                kind="fact",
                content=f"事实 {i}",
                created_at=naive_now(),
            )
        )
    db.commit()


def test_parse_items_tolerates_wrapped_output():
    assert _parse_items("好的，结果如下：\n```json\n[{\"kind\": \"fact\"}]\n```") == [
        {"kind": "fact"}
    ]
    assert _parse_items("") == []
    assert _parse_items("没有值得记的") == []


def test_normalize_collapses_whitespace_and_case():
    assert _normalize("我 在 A  组 ") == _normalize("我在a组")


def test_extract_writes_deduped_items(db):
    adapter = StubAdapter(
        '[{"kind": "fact", "content": "用户负责报销审核"},'
        '{"kind": "preference", "content": "用户偏好中文回答"},'
        '{"kind": "fact", "content": "用户负责  报销审核"},'
        '{"kind": "wrong", "content": "kind 非法"},'
        '{"kind": "fact", "content": ""},'
        + f'{{"kind": "fact", "content": "{"x" * 500}"}}]'
    )
    written = run(
        MemoryService().extract(
            adapter,
            db,
            user_id=USER,
            chat_id="c1",
            question="问题",
            answer="回答",
        )
    )

    # 归一化后重复的一条、kind 非法、空内容、超长都被过滤
    assert written == 2
    contents = [m.content for m in db.query(UserMemory).all()]
    assert contents == ["用户负责报销审核", "用户偏好中文回答"]
    assert adapter.calls and "用户负责报销审核" not in adapter.calls[0]


def test_extract_failure_writes_nothing(db):
    class FailingAdapter:
        async def complete(self, **kwargs):
            raise RuntimeError("llm down")

    written = run(
        MemoryService().extract(
            FailingAdapter(), db, user_id=USER, chat_id="c1",
            question="q", answer="a",
        )
    )

    assert written == 0
    assert db.query(UserMemory).count() == 0


def test_extract_prunes_oldest_beyond_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_MAX_ITEMS", 3)
    _memories(db, 3)

    adapter = StubAdapter('[{"kind": "fact", "content": "新事实"}]')
    run(
        MemoryService().extract(
            adapter, db, user_id=USER, chat_id="c1", question="q", answer="a"
        )
    )

    contents = [m.content for m in db.query(UserMemory).all()]
    # 超量丢最旧:事实 0 被挤掉
    assert "事实 0" not in contents
    assert len(contents) == 3


def test_build_system_block_latest_first_and_empty(db):
    assert MemoryService().build_system_block(db, USER) == ""
    _memories(db, 2)
    block = MemoryService().build_system_block(db, USER)
    assert block.startswith("[用户长期记忆")
    assert "- 事实 1" in block and "- 事实 0" in block
