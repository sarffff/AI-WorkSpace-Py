"""断线接续:孤儿回收、可接续列表、以及"只从轮次边界接"这条安全边界。

最重要的一条是 ``test_断在工具中途不允许接续``。整件事的支点是:``pre_tools`` 的快照
拍在"模型说完、工具还没跑"的位置,从它接上会把本轮工具**重跑一遍**——只读工具重跑
是白花钱,``save_to_knowledge_base`` 重跑就是写第二份。审批那条路径靠 ``writes``
逐步记着"哪几个已经跑完",而断线时 ``writes`` 停在拍快照那一刻,所以那套机制在这里
用不上。因此只有 ``post_tools`` 是安全的接续点。
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

from conftest import run
from config import settings
from models import AgentRun, User
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


@pytest.fixture
def own_session_is_test_db(db_real, monkeypatch):
    """让 ``mark_interrupted`` 自建的会话落到测试库上。

    它刻意不用请求作用域的 db（生成器被回收时那个很可能已经关了，见
    ``checkpoint_store.mark_interrupted`` 的文档串），而是 ``SessionLocal()``
    自己开一个——那指向 ``DATABASE_URL`` 里的 MySQL，测试里连不上也看不到。

    ``close()`` 要吞掉：``mark_interrupted`` 用的是 ``with SessionLocal() as ...``，
    真关掉的话后面的断言就拿着一个关了的 session。
    """
    import database

    class _Borrowed:
        def __enter__(self):
            return db_real

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "SessionLocal", lambda: _Borrowed())
    return db_real


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


# ---- interrupted：断线后立刻可接续 ---------------------------------------


def test_断线立刻可接续不必等超时(db_real, cp_on):
    """静默重连要在几秒内接上，等不了 780 秒的孤儿超时。

    所以 SSE 的 finally 会把它标成 interrupted，而这一档**不看时间**。
    """
    _run_row(db_real, age_seconds=1)
    checkpoint_store.put(db_real, _state())
    assert checkpoint_store.mark_interrupted("run-1", db_real) is True

    rows = checkpoint_store.list_resumable(db_real, "u1")
    assert [row.id for row in rows] == ["run-1"], (
        "标成 interrupted 之后应立刻出现在可接续列表里，不该等超时"
    )


def test_标记不存在的run返回假(db_real, cp_on):
    assert checkpoint_store.mark_interrupted("nope", db_real) is False


def test_interrupted的run能接续(db_real, cp_on):
    from tests.conftest import collect, run
    from services.chat_service import ChatService

    _run_row(db_real, age_seconds=1)
    checkpoint_store.put(db_real, _state())
    checkpoint_store.mark_interrupted("run-1", db_real)

    service = ChatService()
    events = run(collect(service.continue_orphan(db_real, "u1", "run-1")))
    # 不该因为状态不是 running 就拒绝
    errors = [e for e in events if e["type"] == "error"]
    assert not any("不是断线未完成" in e["error"] for e in errors), errors


def test_等审批的不会被标成interrupted(db_real, cp_on):
    """审批中断走的是 waiting_approval。把它改成 interrupted 会让审批卡片
    从待审批列表里消失——用户点开发现要批的东西不见了。"""
    _run_row(db_real, status="waiting_approval", age_seconds=1)
    assert checkpoint_store.mark_interrupted("run-1", db_real) is False
    assert db_real.get(AgentRun, "run-1").status == "waiting_approval"


def test_interrupted超时后一并回收(db_real, cp_on):
    """interrupted 是"可以接续"，不是"永远等着接续"。

    超过同一个时限还没人接，说明用户已经走了。
    """
    _run_row(db_real, age_seconds=10_000)
    checkpoint_store.mark_interrupted("run-1", db_real)
    # 时间要保持在超时之外
    row = db_real.get(AgentRun, "run-1")
    row.updated_at = naive_now() - timedelta(seconds=10_000)
    db_real.commit()

    assert checkpoint_store.reap_orphan_runs(db_real, "u1") == 1
    assert db_real.get(AgentRun, "run-1").status == "failed"


def _drive_then_disconnect(response, *, stop_after: int) -> list[dict]:
    """消费 SSE 响应的前 ``stop_after`` 条事件，然后**关掉它**（= 客户端断线）。

    断线在 ASGI 层的表现就是响应体迭代器被提前 ``aclose()``，生成器于是在
    ``yield`` 处收到 ``GeneratorExit``，``finally`` 随之执行。这正是要验的东西，
    所以这里直接驱动 ``body_iterator`` 而不用 ``TestClient``——后者会把整条流
    读完（那是"正常结束"，一条也测不到断线）。
    """
    seen: list[dict] = []

    async def drive():
        iterator = response.body_iterator.__aiter__()
        try:
            while len(seen) < stop_after:
                chunk = await iterator.__anext__()
                payload = chunk["data"] if isinstance(chunk, dict) else str(chunk)
                try:
                    seen.append(json.loads(payload))
                except (TypeError, json.JSONDecodeError):
                    continue
        except StopAsyncIteration:
            pass
        finally:
            # 提前关闭 = 断线。finally 在这里跑。
            await response.body_iterator.aclose()

    run(drive())
    return seen


def test_sse断线触发finally标记interrupted(
    db_real, cp_on, own_session_is_test_db, monkeypatch
):
    """关键缺口：SSE 生成器的 ``finally`` 在客户端断线时真的执行。

    现有的 ``test_interrupted的run能接续`` 调的是 ``mark_interrupted`` 本身，
    那是单元测试——它证明那个函数能改状态，不证明**有人会调它**。这条从路由层
    驱动：拿到 ``EventSourceResponse``，消费几条事件，然后关掉迭代器。

    **必须测路由层，不能测服务层。** ``mark_interrupted`` 在
    ``chat_router.stream_completions`` 的 ``finally`` 里，``chat_service`` 那一层
    压根没有这个块。改动之前这条测试驱动的是服务层的生成器，于是它就算跑通也
    什么都没验证到——而它连方法名都是错的（``agent_answer`` 不存在）。

    脚本给两轮：第一轮调 calculate（于是有 ``run_started`` / ``tool_start`` 可消费，
    并且会落一份 ``post_tools`` 快照），断在第二轮的模型调用之前。
    """
    import routers.chat_router as chat_router
    from tests.test_sse_contract import make_service
    from tests.test_checkpoint_resume import seed_admin

    monkeypatch.setattr(settings, "AGENT_APPROVAL_MODE", "off")
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "TOOL_CALCULATE_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_WEB_SEARCH_ENABLED", False)
    monkeypatch.setattr(settings, "TOOL_READ_ATTACHMENT_ENABLED", False)
    monkeypatch.setattr(settings, "TOOL_WRITE_KNOWLEDGE_ENABLED", False)
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)

    admin_id = seed_admin(db_real)
    service, _adapter = make_service(
        [
            {"tool_calls": [("calculate", {"expression": "1+1"})]},
            {"text": "等于 2。"},
        ]
    )
    # 路由持有一个模块级 ChatService。要让它用脚本化的适配器,只能换掉那一个。
    monkeypatch.setattr(chat_router, "chat_service", service)

    chat = run(service.create_chat(db_real, user_id=admin_id, title="断线"))
    # ChatRequest.message_id 是 uuid.UUID，不是任意字符串
    message_id = str(uuid.uuid4())
    run(
        service.save_message(
            db_real, chat.id, "user", "算 1+1", "gpt-4o-mini", message_id
        )
    )

    response = run(
        chat_router.stream_completions(
            chat_router.ChatRequest(
                chat_id=chat.id,
                prompt="算 1+1",
                use_rag=False,
                message_id=message_id,
            ),
            db=db_real,
            current_user=db_real.get(User, admin_id),
        )
    )
    # run_started 是第一条带 runId 的事件,刻意早发就是为了断线时有接续凭证。
    events = _drive_then_disconnect(response, stop_after=2)

    run_id = next(
        (event.get("runId") for event in events if event.get("type") == "run_started"),
        None,
    )
    assert run_id, f"没拿到 run_started 的 runId，实际事件：{[e.get('type') for e in events]}"

    db_real.expire_all()
    row = db_real.get(AgentRun, run_id)
    assert row is not None, f"run {run_id} 没落库"
    # 断线之后立刻可接续,不必等 780 秒的孤儿回收。
    assert row.status == "interrupted", f"应标成 interrupted，实际 {row.status}"


def test_恢复流断线也标interrupted(db_real, cp_on, own_session_is_test_db, monkeypatch):
    """断线修复必须覆盖**三个恢复端点**，不只是 /completions/stream。

    ``_continuation_sse`` 起初没有 ``finally``：一次"点了同意、恢复到一半又断了"
    的执行会停在 ``running`` 直到孤儿回收（约 780 秒），而静默重连等不了那么久。
    症状是用户点了同意、看着它转、然后什么都没有，十三分钟内也接不回来。

    这里直接测那个包装函数：给它一个发几条事件的流，消费一条就断。
    """
    import routers.chat_router as chat_router

    _run_row(db_real, status="running")

    async def fake_stream():
        yield {"type": "approval_resolved", "runId": "run-1", "approved": True}
        yield {"type": "message_delta", "content": "已经保存好了。"}

    response = chat_router._continuation_sse(
        fake_stream(),
        db=db_real,
        user_id="u1",
        run_id="run-1",
        chat_id="chat-1",
        assistant_message_id=None,
        prefix="",
        model="gpt-4o-mini",
        state_before=None,
        what="恢复",
    )
    _drive_then_disconnect(response, stop_after=1)

    db_real.expire_all()
    assert db_real.get(AgentRun, "run-1").status == "interrupted"


def test_恢复流正常跑完不标interrupted(db_real, cp_on):
    """``settled`` 的另一半：完整消费掉的流绝不能被标成断线。

    只测上一条的话，一个"永远标 interrupted"的实现也会通过——而那会把每一次
    正常的恢复都变成待接续项，用户每答完一次澄清都看到一个"接着跑吗"的提示。
    """
    import routers.chat_router as chat_router

    _run_row(db_real, status="running")

    async def fake_stream():
        yield {"type": "message_delta", "content": "答完了。"}

    response = chat_router._continuation_sse(
        fake_stream(),
        db=db_real,
        user_id="u1",
        run_id="run-1",
        chat_id="chat-1",
        # 落库要 assistant id,给 None 就只发事件不写库——这条测的是状态,不是落库
        assistant_message_id=None,
        prefix="",
        model="gpt-4o-mini",
        state_before=None,
        what="恢复",
    )
    # 消费到底
    _drive_then_disconnect(response, stop_after=50)

    db_real.expire_all()
    assert db_real.get(AgentRun, "run-1").status == "running"
