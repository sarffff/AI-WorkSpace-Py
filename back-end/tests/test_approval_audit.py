"""审批审计与超时的测试。

挑的是「实现里做过一个具体决定」的点：

- digest 按 key 排序（否则同一份参数算出两个哈希，"批的和执行的是同一份"会假阴性）
- 改过参数时 decided_digest ≠ arguments_digest（审计里最值得看的一行）
- expired 与 rejected 是两个终态，且 expired 的 decided_by 为 NULL
- 一个 run 多次中断时，裁决只落在最新那条 pending 上
- 审计写入失败不能中断审批流程
- 超时的 run 标成 abandoned 而不是 failed
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from config import settings
from models import AgentApproval, AgentRun
from services import approval_audit, checkpoint_store
from services.clock import naive_now


@pytest.fixture
def audit_on(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_APPROVAL_TIMEOUT_HOURS", 24)
    return settings


def _request(db, *, run_id="run-1", user_id="u1", tool="save_to_knowledge_base", args=None):
    return approval_audit.record_request(
        db,
        run_id=run_id,
        chat_id="chat-1",
        user_id=user_id,
        tool_name=tool,
        tool_call_id="call-0",
        round_index=1,
        call_index=0,
        arguments=args if args is not None else {"title": "报销制度", "content": "正文"},
        reason="写操作需确认",
    )


def _run(db, *, run_id="run-1", user_id="u1", status="waiting_approval", age_hours=0.0):
    run = AgentRun(
        id=run_id,
        chat_id="chat-1",
        user_id=user_id,
        status=status,
        rounds=1,
        started_at=naive_now() - timedelta(hours=age_hours),
        updated_at=naive_now() - timedelta(hours=age_hours),
    )
    db.add(run)
    db.commit()
    return run


# ---- digest ---------------------------------------------------------------


def test_digest按key排序(audit_on):
    """键序不同的同一份参数必须算出同一个哈希。

    不排序的话，"批准的和执行的是不是同一份"这个判断会假阴性——而那是这张表
    唯一真正要回答的问题。
    """
    assert approval_audit.digest({"a": 1, "b": 2}) == approval_audit.digest({"b": 2, "a": 1})


def test_digest对内容敏感(audit_on):
    assert approval_audit.digest({"content": "原文"}) != approval_audit.digest(
        {"content": "改过"}
    )


def test_预览截断且写出实际长度(audit_on):
    long_args = {"content": "字" * 3000}
    text = approval_audit.preview(long_args)
    assert len(text) < 3000
    # "原本有多长"本身是审计信息：20000 字符的写入和 200 字符的写入不是一回事
    assert "字符）" in text


def test_预览不截断短参数(audit_on):
    text = approval_audit.preview({"title": "短"})
    assert "…" not in text


def test_digest不因不可序列化而抛(audit_on):
    class Weird:
        pass

    # 不该抛——审计失败不能中断审批
    assert isinstance(approval_audit.digest({"x": Weird()}), str)


# ---- 请求与裁决 -----------------------------------------------------------


def test_登记请求写出pending(db_real, audit_on):
    record_id = _request(db_real)
    assert record_id is not None
    record = db_real.get(AgentApproval, record_id)
    assert record.decision == "pending"
    assert record.decided_by is None
    assert record.decided_at is None
    assert record.arguments_digest is not None
    assert record.tool_name == "save_to_knowledge_base"


def test_批准记录人与时间(db_real, audit_on):
    _request(db_real)
    assert approval_audit.record_decision(
        db_real, run_id="run-1", decided_by="u1", approved=True
    )
    record = db_real.query(AgentApproval).filter_by(run_id="run-1").one()
    assert record.decision == "approved"
    assert record.decided_by == "u1"
    assert record.decided_at is not None


def test_拒绝留下理由(db_real, audit_on):
    _request(db_real)
    approval_audit.record_decision(
        db_real,
        run_id="run-1",
        decided_by="u1",
        approved=False,
        note="别写进知识库，先给我看看",
    )
    record = db_real.query(AgentApproval).filter_by(run_id="run-1").one()
    assert record.decision == "rejected"
    assert "先给我看看" in record.note


def test_改过参数时两个digest不同(db_real, audit_on):
    """审计里最值得看的一行：执行的不是模型原本要执行的东西。"""
    _request(db_real, args={"title": "原标题", "content": "正文"})
    approval_audit.record_decision(
        db_real,
        run_id="run-1",
        decided_by="u1",
        approved=True,
        effective_arguments={"title": "改后的标题", "content": "正文"},
        edited_fields=["title"],
    )
    record = db_real.query(AgentApproval).filter_by(run_id="run-1").one()
    assert record.decided_digest != record.arguments_digest
    assert record.edited_fields == "title"
    entry = approval_audit.history(db_real, "run-1")[0]
    assert entry["argumentsEdited"] is True
    assert entry["editedFields"] == ["title"]


def test_没改参数时不算编辑过(db_real, audit_on):
    _request(db_real, args={"title": "原标题"})
    approval_audit.record_decision(
        db_real,
        run_id="run-1",
        decided_by="u1",
        approved=True,
        effective_arguments={"title": "原标题"},
    )
    entry = approval_audit.history(db_real, "run-1")[0]
    assert entry["argumentsEdited"] is False


def test_多次中断裁决落在最新那条(db_real, audit_on):
    """一次执行可以被打断多次，每次裁决只对应一次中断。

    第一次拒绝、改了参数第二次才批——这正是最需要留痕的形状。
    """
    first = _request(db_real, args={"title": "第一次"})
    approval_audit.record_decision(
        db_real, run_id="run-1", decided_by="u1", approved=False, note="不行"
    )
    # 人为把第一条的时间往前挪，好让排序确定
    db_real.get(AgentApproval, first).requested_at = naive_now() - timedelta(minutes=5)
    db_real.commit()

    second = _request(db_real, args={"title": "第二次"})
    approval_audit.record_decision(
        db_real, run_id="run-1", decided_by="u1", approved=True
    )

    assert db_real.get(AgentApproval, first).decision == "rejected"
    assert db_real.get(AgentApproval, second).decision == "approved"
    assert len(approval_audit.history(db_real, "run-1")) == 2


def test_没有pending时裁决不报错(db_real, audit_on):
    """审批开关中途打开时，历史 run 没有对应的 pending 记录。"""
    assert (
        approval_audit.record_decision(
            db_real, run_id="run-never", decided_by="u1", approved=True
        )
        is False
    )


def test_审计失败不抛异常(db_real, audit_on, monkeypatch):
    """审计是记录不是闸门。写入失败不能让用户的写操作也一起挡掉。"""

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_real, "commit", _boom)
    monkeypatch.setattr(db_real, "rollback", lambda: None)
    assert _request(db_real) is None


# ---- 超时 -----------------------------------------------------------------


def test_超时判定看小时数(audit_on, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_APPROVAL_TIMEOUT_HOURS", 24)
    assert approval_audit.is_expired(naive_now() - timedelta(hours=25)) is True
    assert approval_audit.is_expired(naive_now() - timedelta(hours=23)) is False


def test_时限为零表示不过期(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_APPROVAL_TIMEOUT_HOURS", 0)
    assert approval_audit.is_expired(naive_now() - timedelta(days=365)) is False


def test_过期记录标expired且无裁决人(db_real, audit_on):
    """expired 与 rejected 必须分开：拒绝是人做的决定，过期是没人做决定。"""
    record_id = _request(db_real)
    db_real.get(AgentApproval, record_id).requested_at = naive_now() - timedelta(hours=25)
    db_real.commit()

    assert approval_audit.expire_stale(db_real) == 1
    record = db_real.get(AgentApproval, record_id)
    assert record.decision == "expired"
    # 「没有人做这个决定」的准确表示
    assert record.decided_by is None
    assert record.decided_at is None


def test_过期扫描不动已裁决的(db_real, audit_on):
    record_id = _request(db_real)
    approval_audit.record_decision(
        db_real, run_id="run-1", decided_by="u1", approved=True
    )
    db_real.get(AgentApproval, record_id).requested_at = naive_now() - timedelta(hours=99)
    db_real.commit()
    assert approval_audit.expire_stale(db_real) == 0
    assert db_real.get(AgentApproval, record_id).decision == "approved"


def test_超时的run标abandoned而不是failed(db_real, audit_on):
    """这次执行没有出错，是没人来裁决。

    标成 failed 会让「错误率」这个指标把无人处理的审批也算成故障。
    """
    _run(db_real, age_hours=25)
    assert checkpoint_store.expire_stale_runs(db_real, "u1") == 1
    run = db_real.get(AgentRun, "run-1")
    assert run.status == "abandoned"
    assert run.error_type == "approval_timeout"
    assert run.finished_at is not None


def test_未超时的run不动(db_real, audit_on):
    _run(db_real, age_hours=1)
    assert checkpoint_store.expire_stale_runs(db_real, "u1") == 0
    assert db_real.get(AgentRun, "run-1").status == "waiting_approval"


def test_运行中的run不受时限影响(db_real, audit_on):
    """只有 waiting_* 才会过期。running 是有进程在跑它的。"""
    _run(db_real, status="running", age_hours=99)
    assert checkpoint_store.expire_stale_runs(db_real, "u1") == 0
    assert db_real.get(AgentRun, "run-1").status == "running"


def test_等待输入的run也会过期(db_real, audit_on):
    _run(db_real, status="waiting_input", age_hours=25)
    assert checkpoint_store.expire_stale_runs(db_real, "u1") == 1
    assert db_real.get(AgentRun, "run-1").status == "abandoned"


def test_过期扫描按用户隔离(db_real, audit_on):
    _run(db_real, run_id="run-a", user_id="u1", age_hours=25)
    _run(db_real, run_id="run-b", user_id="u2", age_hours=25)
    assert checkpoint_store.expire_stale_runs(db_real, "u1") == 1
    assert db_real.get(AgentRun, "run-b").status == "waiting_approval"


def test_时限为零时不扫描(db_real, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_APPROVAL_TIMEOUT_HOURS", 0)
    _run(db_real, age_hours=999)
    assert checkpoint_store.expire_stale_runs(db_real, "u1") == 0
    assert approval_audit.expire_stale(db_real) == 0
