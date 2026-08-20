"""长期记忆的单元测试。

风险集中在两处：抽取的去重（同一句话反复出现不该变成两行记忆），以及注入的
安全性——记忆以 role=system 注入、权限比任何检索内容都高，而内容来自对话历史，
检测挡不住措辞正常的假偏好，防线只能是结构性的（定界 + 声明没有指令权限）。

抽取侧的契约（kind 取值、content 长度）现在由 ``services.structured.MemoryItem``
声明并强制：不合法的一批会触发一次带报错的重试，而不是静默丢掉不合法的项。
"""
from __future__ import annotations

import json

from conftest import ScriptedAdapter, run
from models import UserMemory
from services.clock import naive_now
from services.memory_service import MemoryService, _normalize
from services.structured import extract_json


class PurposeAwareAdapter(ScriptedAdapter):
    """ScriptedAdapter 的 complete 不收 purpose，而抽取路径会传。"""

    async def complete(
        self, *, messages, tools, model, temperature=0.7, max_tokens=2048, top_p=1.0, purpose="chat"
    ):
        return await super().complete(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
        )


def _json_round(items: list[dict]) -> dict:
    """一轮返回合法 JSON 的脚本。

    以前这里用的是 ``str(items)``——Python 的 repr 是单引号，不是合法 JSON，
    于是解析必然失败、一条都写不进去，而那几个测试只断言了"调用发生过一次",
    所以一直是绿的。
    """
    return {"text": json.dumps(items, ensure_ascii=False)}


# ========== 归一化与解析 ==========


def test_normalize_collapses_whitespace_and_case():
    """中文书写没有空格，用户随口多打一个空格不该绕过去重。"""
    assert _normalize(" 我  爱 北京 ") == "我爱北京"
    assert _normalize("Hello World") == "helloworld"
    assert _normalize("HELLO world") == _normalize("hello WORLD")


def test_extract_json_pulls_first_array():
    assert extract_json('[{"content": "a"}]', array=True) == [{"content": "a"}]
    assert extract_json('好的，我提取了：[{"content": "a"}]', array=True) == [
        {"content": "a"}
    ]
    assert extract_json('```json\n[{"content": "a"}]\n```', array=True) == [
        {"content": "a"}
    ]
    assert extract_json("没有提取到", array=True) is None
    # 契约要数组，给了对象就是真的错了，不该被当成成功
    assert extract_json('{"content": "a"}', array=True) is None


# ========== 抽取 ==========


class _MemoryDB:
    """抽取路径需要的极小 Session 替身（只断言写入的条数）。"""

    def __init__(self) -> None:
        self.added: list[UserMemory] = []

    def query(self, *args, **kwargs):
        return _EmptyQuery()

    def add(self, entity) -> None:
        self.added.append(entity)

    def commit(self) -> None:
        return None


class _EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return []

    def count(self):
        return 0


def test_extract_writes_valid_kinds():
    db = _MemoryDB()
    adapter = PurposeAwareAdapter(
        [
            _json_round(
                [
                    {"kind": "fact", "content": "用户是销售"},
                    {"kind": "preference", "content": "回答要简洁"},
                ]
            )
        ]
    )
    written = run(
        MemoryService().extract(
            adapter, db, user_id="u1", chat_id="c1", question="q", answer="a"
        )
    )
    # 一次就给对了,不该有重试
    assert len(adapter.calls) == 1
    assert written == 2
    assert {row.kind for row in db.added} == {"fact", "preference"}


def test_extract_retries_on_invalid_kind_then_gives_up(monkeypatch):
    """不合法的一批会重试一次,两次都不合法就整批放弃。

    改动之前这里是"逐项 if 跳过":``kind: evil`` 被静默丢掉,而那等于把
    "模型没照指令做"翻译成"这轮没什么可记的",后者不会有人去查。
    """
    from config import settings

    monkeypatch.setattr(settings, "STRUCTURED_OUTPUT_RETRIES", 1)
    db = _MemoryDB()
    bad = _json_round([{"kind": "evil", "content": "x"}])
    adapter = PurposeAwareAdapter([bad, bad])
    written = run(
        MemoryService().extract(
            adapter, db, user_id="u1", chat_id="c1", question="q", answer="a"
        )
    )
    assert written == 0
    assert db.added == []
    # 两次调用 = 首次 + 一次重试
    assert len(adapter.calls) == 2
    # 重试那一轮必须带上上次的输出和校验报错，否则模型无从下手
    retry_messages = adapter.calls[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"
    assert "校验报错" in retry_messages[-1]["content"]


def test_extract_recovers_when_retry_returns_valid(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "STRUCTURED_OUTPUT_RETRIES", 1)
    db = _MemoryDB()
    adapter = PurposeAwareAdapter(
        [
            _json_round([{"kind": "evil", "content": "x"}]),
            _json_round([{"kind": "fact", "content": "用户是销售"}]),
        ]
    )
    written = run(
        MemoryService().extract(
            adapter, db, user_id="u1", chat_id="c1", question="q", answer="a"
        )
    )
    assert written == 1
    assert db.added[0].content == "用户是销售"


def test_extract_rejects_oversized_items(monkeypatch):
    """超长的"记忆"多半是把整段对话抄了一遍,是不照做而不是没什么可记。"""
    from config import settings

    monkeypatch.setattr(settings, "MEMORY_ITEM_MAX_CHARS", 10)
    monkeypatch.setattr(settings, "STRUCTURED_OUTPUT_RETRIES", 0)
    db = _MemoryDB()
    adapter = PurposeAwareAdapter(
        [_json_round([{"kind": "fact", "content": "x" * 20}])]
    )
    written = run(
        MemoryService().extract(
            adapter, db, user_id="u1", chat_id="c1", question="q", answer="a"
        )
    )
    assert written == 0
    assert db.added == []


def test_extract_call_failure_does_not_retry():
    """调用本身失败(超时/限流)重试同一段提示词没有意义:错不在格式上。"""
    db = _MemoryDB()
    adapter = PurposeAwareAdapter([{"raise": True}])
    written = run(
        MemoryService().extract(
            adapter, db, user_id="u1", chat_id="c1", question="q", answer="a"
        )
    )
    assert written == 0
    assert len(adapter.calls) == 1


# ========== 落库去重与裁剪 ==========


def test_extract_dedupes_by_normalized_content(db_real):
    service = MemoryService()
    adapter = PurposeAwareAdapter(
        [_json_round([{"kind": "fact", "content": "用户是销售"}])]
    )
    first = run(
        service.extract(adapter, db_real, user_id="u1", chat_id="c1", question="q", answer="a")
    )
    assert first == 1

    adapter = PurposeAwareAdapter(
        [_json_round([{"kind": "fact", "content": "用户是 销售"}])]
    )
    second = run(
        service.extract(adapter, db_real, user_id="u1", chat_id="c1", question="q", answer="a")
    )
    assert second == 0
    assert (
        db_real.query(UserMemory).filter(UserMemory.user_id == "u1").count() == 1
    )


def test_prune_drops_oldest(monkeypatch, db_real):
    from config import settings

    monkeypatch.setattr(settings, "MEMORY_MAX_ITEMS", 2)
    service = MemoryService()
    adapter = PurposeAwareAdapter(
        [
            _json_round(
                [
                    {"kind": "fact", "content": "第一条"},
                    {"kind": "fact", "content": "第二条"},
                    {"kind": "fact", "content": "第三条"},
                ]
            )
        ]
    )
    run(
        service.extract(adapter, db_real, user_id="u1", chat_id="c1", question="q", answer="a")
    )

    rows = db_real.query(UserMemory).filter(UserMemory.user_id == "u1").all()
    assert len(rows) == 2
    assert {row.content for row in rows} == {"第二条", "第三条"}


# ========== 注入 ==========


def test_build_system_block_empty_without_memories(db_real):
    assert MemoryService().build_system_block(db_real, "u1") == ""


def _seed_memory(db, user_id: str, content: str) -> None:
    db.add(UserMemory(user_id=user_id, kind="fact", content=content, created_at=naive_now()))


def test_build_system_block_uses_fence_with_guard(db_real):
    _seed_memory(db_real, "u1", "用户是销售")
    block = MemoryService().build_system_block(db_real, "u1")
    assert "用户长期记忆" in block
    assert "开始" in block
    # 记忆以 role=system 注入、权限最高，声明必须明确它没有指令权限
    assert "不是操作指令" in block
    assert "用户是销售" in block


def test_build_system_block_plain_without_guard(monkeypatch, db_real):
    from config import settings

    monkeypatch.setattr(settings, "GUARDRAIL_ENABLED", False)
    _seed_memory(db_real, "u1", "用户是销售")
    block = MemoryService().build_system_block(db_real, "u1")
    assert "[用户长期记忆" in block
    # guard 关掉时没有 nonce 定界，也没有"不是操作指令"的声明
    assert "开始" not in block
    assert "不是操作指令" not in block


def test_build_system_block_respects_inject_limit(monkeypatch, db_real):
    from config import settings

    monkeypatch.setattr(settings, "MEMORY_INJECT_LIMIT", 1)
    _seed_memory(db_real, "u1", "旧的")
    _seed_memory(db_real, "u1", "新的")
    block = MemoryService().build_system_block(db_real, "u1")
    assert "新的" in block
    assert "旧的" not in block