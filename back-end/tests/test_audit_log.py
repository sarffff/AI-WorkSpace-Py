"""审计链的测试：追加→链接、verify 检出篡改与删除、链按 actor 分段。

用 db_real（内存 SQLite 真建表）而不是 FakeDB：这里要断言的正是持久化后的 seq、
prev_hash 链接与 verify 走链的结果，替身全测不出来。
"""
from __future__ import annotations

from models import AuditLog
from services import audit_log


def _rows(db, actor_id: str):
    return (
        db.query(AuditLog)
        .filter(AuditLog.actor_id == actor_id)
        .order_by(AuditLog.seq.asc())
        .all()
    )


def test_records_chain_links(db_real):
    a = audit_log.record(db_real, actor_id="u1", action="auth.login", target="u1@x")
    b = audit_log.record(
        db_real, actor_id="u1", action="tool.write", target="write_file",
        arguments={"path": "a.txt"},
    )
    assert a and b
    rows = _rows(db_real, "u1")
    assert [r.seq for r in rows] == [1, 2]
    # 首条挂 genesis，第二条挂第一条的 entry_hash
    assert rows[0].prev_hash == audit_log.GENESIS_HASH
    assert rows[1].prev_hash == rows[0].entry_hash


def test_verify_ok_on_intact_chain(db_real):
    for i in range(3):
        audit_log.record(db_real, actor_id="u1", action="tool.write", target=f"t{i}")
    assert audit_log.verify(db_real, "u1") == {
        "ok": True,
        "entries": 3,
        "firstBrokenSeq": None,
    }


def test_verify_detects_tampering(db_real):
    audit_log.record(db_real, actor_id="u1", action="tool.write", target="write_file")
    audit_log.record(
        db_real, actor_id="u1", action="approval.approved",
        target="save_to_knowledge_base",
    )
    audit_log.record(db_real, actor_id="u1", action="tool.write", target="delete_file")
    # 事后把中间那条的 target 改掉（"批了保存"改成"批了删除"）：它的 entry_hash
    # 不再覆盖新内容，重算对不上
    victim = (
        db_real.query(AuditLog)
        .filter(AuditLog.actor_id == "u1", AuditLog.seq == 2)
        .one()
    )
    victim.target = "delete_knowledge_document"
    db_real.commit()
    result = audit_log.verify(db_real, "u1")
    assert result["ok"] is False
    assert result["firstBrokenSeq"] == 2


def test_verify_detects_deletion(db_real):
    for i in range(3):
        audit_log.record(db_real, actor_id="u1", action="tool.write", target=f"t{i}")
    # 删掉中间一条：第三条的 prev_hash 现在挂空了
    db_real.query(AuditLog).filter(
        AuditLog.actor_id == "u1", AuditLog.seq == 2
    ).delete()
    db_real.commit()
    result = audit_log.verify(db_real, "u1")
    assert result["ok"] is False
    assert result["firstBrokenSeq"] == 3


def test_chain_is_per_actor(db_real):
    audit_log.record(db_real, actor_id="u1", action="auth.login", target="u1")
    audit_log.record(db_real, actor_id="u2", action="auth.login", target="u2")
    audit_log.record(db_real, actor_id="u1", action="tool.write", target="write_file")
    assert [r.seq for r in _rows(db_real, "u1")] == [1, 2]
    assert [r.seq for r in _rows(db_real, "u2")] == [1]
    # 两条链各自完整，且 u2 的首条也挂 genesis（不受 u1 影响）
    assert audit_log.verify(db_real, "u1")["ok"] is True
    assert audit_log.verify(db_real, "u2")["ok"] is True
    assert _rows(db_real, "u2")[0].prev_hash == audit_log.GENESIS_HASH


def test_arguments_digest_and_preview_recorded(db_real):
    audit_log.record(
        db_real, actor_id="u1", action="tool.write",
        target="save_to_knowledge_base",
        arguments={"title": "报销制度", "content": "正文"},
    )
    row = db_real.query(AuditLog).filter(AuditLog.actor_id == "u1").one()
    assert row.args_digest and len(row.args_digest) == 64
    assert "报销制度" in (row.args_preview or "")
