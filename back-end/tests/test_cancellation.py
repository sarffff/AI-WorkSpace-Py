"""执行取消（用户主动"停止生成"）的行为测试。

分三层，从里到外：

1. **进程内信号表**：register/signal/is_cancelled/unregister 的语义。
2. **循环收尾**：给定模型每轮都要调工具的剧本，中途发取消，循环必须在下一个
   安全点停下来、发 ``cancelled`` 事件、且不再多跑一轮。
3. **落库状态**：``mark_cancelled`` 只改可取消的状态（running/interrupted），
   不碰终态与等审批的执行。

HTTP 端点（POST /chats/runs/{id}/cancel）的测试在 test_router_integration.py，
因为它要复用那边的 client / 鉴权 fixture。
"""
from __future__ import annotations

from typing import Any

from conftest import FakeKnowledgeService, ScriptedAdapter, run
from config import settings
from services import cancellation
from services.chat_service import ChatService


def make_service(
    rounds: list[dict[str, Any]],
    knowledge: FakeKnowledgeService | None = None,
) -> tuple[ChatService, ScriptedAdapter]:
    adapter = ScriptedAdapter(rounds)
    service = ChatService(model_adapter=adapter)
    service._knowledge_service = knowledge or FakeKnowledgeService()
    return service, adapter


# ========== 1. 进程内信号表 ==========


def test_register_then_signal_is_observed():
    """登记过的 run 收到 signal 后，is_cancelled 立刻为真。"""
    cancellation.register("r1")
    try:
        assert cancellation.is_cancelled("r1") is False
        assert cancellation.signal("r1") is True
        assert cancellation.is_cancelled("r1") is True
    finally:
        cancellation.unregister("r1")
    # 摘除之后再查是"没被取消"，signal 也找不到活着的循环
    assert cancellation.is_cancelled("r1") is False
    assert cancellation.signal("r1") is False


def test_signal_without_register_returns_false():
    """没登记过 = 本进程没有在跑它。调用方据此退回落库那条路。"""
    assert cancellation.signal("never-registered") is False
    assert cancellation.is_cancelled("never-registered") is False


def test_register_is_idempotent_and_preserves_a_prior_signal():
    """重复登记拿到同一个事件；先 signal 再 register 不能把已置位的状态冲掉。

    这钉住 register 用 setdefault 而不是覆盖：极窄竞态下（取消赶在循环登记之前
    到达）那次取消不能丢。
    """
    first = cancellation.register("r2")
    cancellation.signal("r2")
    second = cancellation.register("r2")
    try:
        assert first is second
        assert cancellation.is_cancelled("r2") is True
    finally:
        cancellation.unregister("r2")


# ========== 2. 循环在安全点收尾 ==========


def test_取消信号让循环在下一个安全点收尾(db, monkeypatch):
    """中途发取消，循环不再多跑一轮：发 cancelled 事件、终止、不吐后续正文。

    剧本是"每轮都要检索"，所以只要循环还活着就会一轮轮烧下去。取消之后它必须
    停在轮首的安全点，rounds_used 定格在已经跑过的那一轮。
    """
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    # run_started 事件（携带 runId）只在开了 checkpoint 时才发，测试要靠它拿到
    # run_id 才能 signal。用 FakeDB 即可：落库全是空操作，循环照跑、事件照发。
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    service, adapter = make_service(
        [
            {"tool_calls": [("search_knowledge_base", {"query": "a"})]},
            {"tool_calls": [("search_knowledge_base", {"query": "b"})]},
            {"text": "这段正文不该被生成到"},
        ],
        FakeKnowledgeService(context="ctx"),
    )

    async def drive() -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        run_id: str | None = None
        agen = service.stream_ai_response(db, "u1", "c1", "q", use_rag=True)
        async for event in agen:
            events.append(event)
            if event["type"] == "run_started":
                run_id = event["runId"]
            # 第一轮工具一跑完就发取消。下一次进循环该在轮首收尾。
            if event["type"] == "tool_result" and run_id:
                cancellation.signal(run_id)
        return events

    events = run(drive())
    types = [event["type"] for event in events]

    assert "cancelled" in types, types
    # 只跑了第一轮就停了：没有第二轮的模型调用，也没有那段最终正文
    assert adapter.rounds_used == 1
    assert all(
        "这段正文不该被生成到" not in event.get("content", "") for event in events
    )
    # cancelled 是最后一个事件（循环随即 return）
    assert types[-1] == "cancelled"


def test_取消后循环不再登记泄露(db, monkeypatch):
    """收尾之后 register 的令牌必须被摘掉，否则这张表随执行数无限增长。"""
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    service, _adapter = make_service([{"text": "答案"}])

    seen: dict[str, Any] = {}

    async def drive() -> None:
        run_id = None
        async for event in service.stream_ai_response(db, "u1", "c1", "q"):
            if event["type"] == "run_started":
                run_id = event["runId"]
        seen["run_id"] = run_id

    run(drive())
    # 循环自然结束后，那个 run 不该还挂在进程内的表里
    assert seen["run_id"] is not None
    assert seen["run_id"] not in cancellation.live_runs()


# ========== 3. 落库状态 ==========


def _make_run(db_real, *, run_id: str, status: str, user_id: str = "u1"):
    from models import AgentRun
    from services.clock import naive_now

    db_real.add(
        AgentRun(
            id=run_id,
            chat_id="c1",
            user_id=user_id,
            status=status,
            rounds=1,
            started_at=naive_now(),
            updated_at=naive_now(),
        )
    )
    db_real.commit()


def test_mark_cancelled_accepts_running_and_interrupted(db_real, monkeypatch):
    """running 与 interrupted 都能被推成 cancelled，并记 user_cancelled。

    interrupted 也要接受：前端"先 abort 再取消"时，断线那道会先把它标成
    interrupted，取消这道必须能把它继续推成终态 cancelled。
    """
    from services import checkpoint_store

    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    _make_run(db_real, run_id="run-running", status="running")
    _make_run(db_real, run_id="run-interrupted", status="interrupted")

    assert checkpoint_store.mark_cancelled("run-running", db=db_real) is True
    assert checkpoint_store.mark_cancelled("run-interrupted", db=db_real) is True

    from models import AgentRun

    for rid in ("run-running", "run-interrupted"):
        row = db_real.query(AgentRun).filter_by(id=rid).one()
        assert row.status == "cancelled"
        assert row.error_type == "user_cancelled"
        assert row.finished_at is not None


def test_mark_cancelled_leaves_terminal_and_waiting_states_alone(db_real, monkeypatch):
    """终态与等审批的执行不能被"停止生成"顺手取消。

    done/failed/abandoned 已经结束；waiting_approval/waiting_input 有自己的裁决
    路径。把它们改成 cancelled 会让"取消率"这个指标被无关状态污染，也会让一个
    正在等人点同意的写操作凭空消失。
    """
    from services import checkpoint_store
    from models import AgentRun

    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    for status in ("done", "failed", "abandoned", "waiting_approval", "waiting_input"):
        rid = f"run-{status}"
        _make_run(db_real, run_id=rid, status=status)
        assert checkpoint_store.mark_cancelled(rid, db=db_real) is False
        assert db_real.query(AgentRun).filter_by(id=rid).one().status == status


def test_mark_cancelled_missing_run_returns_false(db_real, monkeypatch):
    from services import checkpoint_store

    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    assert checkpoint_store.mark_cancelled("does-not-exist", db=db_real) is False


# ========== 4. 跨 worker 兜底：循环查落库的 cancelled 状态 ==========


def test_is_run_cancelled_reads_db_status(db_real, monkeypatch):
    """取消打到别的 worker 时，本进程没有 Event，但循环查 DB 能看到 cancelled。

    这是 cancellation.is_cancelled（只看进程内 Event）的跨 worker 兜底：另一个
    worker 的 mark_cancelled 落了 agent_runs.status，这里据此让循环收尾。
    """
    from services import checkpoint_store

    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    _make_run(db_real, run_id="run-cxl", status="cancelled")
    _make_run(db_real, run_id="run-live", status="running")

    assert checkpoint_store.is_run_cancelled(db_real, "run-cxl") is True
    assert checkpoint_store.is_run_cancelled(db_real, "run-live") is False
    # 没有这个 run（例如还没落库）按未取消处理，不炸
    assert checkpoint_store.is_run_cancelled(db_real, "no-such-run") is False


def test_is_run_cancelled_false_when_checkpoints_disabled(db_real, monkeypatch):
    """关掉快照就没有 agent_runs 行、也没有多 worker——直接 False，不查库。"""
    from services import checkpoint_store

    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", False)
    _make_run(db_real, run_id="run-cxl2", status="cancelled")
    assert checkpoint_store.is_run_cancelled(db_real, "run-cxl2") is False
