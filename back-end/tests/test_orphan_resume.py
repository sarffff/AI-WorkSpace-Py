"""断线接续:孤儿回收、可接续列表、以及"只从轮次边界接"这条安全边界。

最重要的一条是 ``test_断在工具中途不允许接续``。整件事的支点是:``pre_tools`` 的快照
拍在"模型说完、工具还没跑"的位置,从它接上会把本轮工具**重跑一遍**——只读工具重跑
是白花钱,``save_to_knowledge_base`` 重跑就是写第二份。审批那条路径靠 ``writes``
逐步记着"哪几个已经跑完",而断线时 ``writes`` 停在拍快照那一刻,所以那套机制在这里
用不上。因此只有 ``post_tools`` 是安全的接续点。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from config import settings
from models import AgentRun
from services import checkpoint_store
from services.agent_state import TurnState
from services.clock import naive_now


@pytest.fixture
def cp_on(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_KEEP", 8)
    monkeypatch.setattr(settings, "LLM_CHAT_TIMEOUT_SECONDS", 120.0)
    monkeypatch.setattr(settings, "LLM_CHAT_MAX_RETRIES", 2)
    return settings


def _run_row(db, *, run_id="run-1", user_id="u1", status="running", age_seconds=0.0):
    when = naive_now() - timedelta(seconds=age_seconds)
    row = AgentRun(
        id=run_id,
        chat_id="chat-1",
        user_id=user_id,
        message_id="m-1",
        status=status,
        rounds=2,
        started_at=when,
        updated_at=when,
    )
    db.add(row)
    db.commit()
    return row


def _state(*, run_id="run-1", user_id="u1", phase="post_tools", round_index=2):
    return TurnState(
        run_id=run_id,
        chat_id="chat-1",
        user_id=user_id,
        workspace_id="w1",
        message_id="m-1",
        phase=phase,
        status="running",
        round_index=round_index,
        messages=[{"role": "user", "content": "算一下"}],
        pending_calls=(
            [] if phase == "post_tools" else [{"id": "c0", "name": "search_knowledge", "arguments": "{}"}]
        ),
    )


# ---- 超时是推导出来的 -----------------------------------------------------


def test_孤儿超时随模型超时一起变(cp_on, monkeypatch):
    """不新加配置项,从 LLM_CHAT_* 推。

    自己拍一个 AGENT_ORPHAN_TIMEOUT 的问题是它会和超时配置各自漂移:有人把
    LLM_CHAT_TIMEOUT_SECONDS 调到 600,孤儿判定还停在原来的值,于是正常运行的
    回合会被当成孤儿标掉。
    """
    base = checkpoint_store.orphan_timeout_seconds()
    monkeypatch.setattr(settings, "LLM_CHAT_TIMEOUT_SECONDS", 600.0)
    assert checkpoint_store.orphan_timeout_seconds() > base


def test_孤儿超时宽于一轮最坏耗时(cp_on):
    """只要比"一轮真的可能花多久"宽,就不会把正在跑的 run 误判成孤儿。"""
    worst_round = settings.LLM_CHAT_TIMEOUT_SECONDS * (settings.LLM_CHAT_MAX_RETRIES + 1)
    assert checkpoint_store.orphan_timeout_seconds() > worst_round


# ---- 孤儿回收 -------------------------------------------------------------


def test_没人驱动的running标failed(db_real, cp_on):
    """SSE 断掉时驱动循环的生成器直接被回收,_finish_run 不会执行。

    终态是 failed/orphaned 而不是 abandoned:这次执行确实异常结束了(连接断了),
    与"没人来裁决"不是一回事。分开之后"断线率"和"审批积压"是两个能分别查的数。
    """
    _run_row(db_real, age_seconds=10_000)
    assert checkpoint_store.reap_orphan_runs(db_real, "u1") == 1
    row = db_real.get(AgentRun, "run-1")
    assert row.status == "failed"
    assert row.error_type == "orphaned"
    assert row.finished_at is not None


def test_刚动过的running不回收(db_real, cp_on):
    _run_row(db_real, age_seconds=5)
    assert checkpoint_store.reap_orphan_runs(db_real, "u1") == 0
    assert db_real.get(AgentRun, "run-1").status == "running"


def test_等审批的不算孤儿(db_real, cp_on):
    """等审批是**刻意**让它活着(状态在库里,等另一个请求接上)。

    把它当孤儿回收掉,等于用户点开审批列表时发现要批的东西自己消失了。
    """
    _run_row(db_real, status="waiting_approval", age_seconds=10_000)
    assert checkpoint_store.reap_orphan_runs(db_real, "u1") == 0
    assert db_real.get(AgentRun, "run-1").status == "waiting_approval"


def test_孤儿回收按用户隔离(db_real, cp_on):
    _run_row(db_real, run_id="run-a", user_id="u1", age_seconds=10_000)
    _run_row(db_real, run_id="run-b", user_id="u2", age_seconds=10_000)
    assert checkpoint_store.reap_orphan_runs(db_real, "u1") == 1
    assert db_real.get(AgentRun, "run-b").status == "running"


# ---- 可接续列表 -----------------------------------------------------------


def test_列出断线且停在轮次边界的(db_real, cp_on):
    _run_row(db_real, age_seconds=10_000)
    checkpoint_store.put(db_real, _state(phase="post_tools"))
    rows = checkpoint_store.list_resumable(db_real, "u1")
    assert [row.id for row in rows] == ["run-1"]


def test_子代理run不进可接续列表(db_real, cp_on):
    """子代理跑在父代理的一次 delegate 调用内部,没有独立的接续入口。"""
    row = _run_row(db_real, run_id="run-child", age_seconds=10_000)
    row.parent_run_id = "run-parent"
    db_real.commit()
    assert checkpoint_store.list_resumable(db_real, "u1") == []


def test_刚动过的不进可接续列表(db_real, cp_on):
    _run_row(db_real, age_seconds=5)
    checkpoint_store.put(db_real, _state())
    assert checkpoint_store.list_resumable(db_real, "u1") == []


# ---- 安全边界:只从 post_tools 接 -----------------------------------------


def test_断在工具中途不允许接续(db_real, cp_on):
    """整件事的支点。

    ``pre_tools`` 的快照拍在"模型说完、工具还没跑"的位置。从它接上,本轮工具会
    重跑一遍:只读工具重跑是白花钱,save_to_knowledge_base 重跑就是写第二份。

    断线时没有任何记录说明"那一轮里哪几个工具真的跑完了"——审批路径靠 writes
    逐步记账,而断线时 writes 停在拍快照那一刻。所以这里必须拒绝,而不是猜。
    """
    from tests.conftest import collect, run
    from services.chat_service import ChatService

    _run_row(db_real, age_seconds=10_000)
    checkpoint_store.put(db_real, _state(phase="pre_tools"))

    service = ChatService()
    events = run(collect(service.continue_orphan(db_real, "u1", "run-1")))
    errors = [e for e in events if e["type"] == "error"]
    assert errors, "断在工具中途必须拒绝接续"
    assert "重复写入" in errors[0]["error"]
    # 并且就地标掉:留在 running 里会让它在可接续列表和孤儿回收之间反复出现
    assert db_real.get(AgentRun, "run-1").error_type == "unsafe_resume_point"


def test_没有快照不允许接续(db_real, cp_on):
    from tests.conftest import collect, run
    from services.chat_service import ChatService

    _run_row(db_real, age_seconds=10_000)
    service = ChatService()
    events = run(collect(service.continue_orphan(db_real, "u1", "run-1")))
    assert [e for e in events if e["type"] == "error"]


def test_别人的run接不了(db_real, cp_on):
    from tests.conftest import collect, run
    from services.chat_service import ChatService

    _run_row(db_real, user_id="u2", age_seconds=10_000)
    checkpoint_store.put(db_real, _state(user_id="u2"))
    service = ChatService()
    events = run(collect(service.continue_orphan(db_real, "u1", "run-1")))
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "不属于当前用户" in errors[0]["error"]


def test_已完成的run接不了(db_real, cp_on):
    from tests.conftest import collect, run
    from services.chat_service import ChatService

    _run_row(db_real, status="done", age_seconds=10_000)
    service = ChatService()
    events = run(collect(service.continue_orphan(db_real, "u1", "run-1")))
    assert [e for e in events if e["type"] == "error"]
