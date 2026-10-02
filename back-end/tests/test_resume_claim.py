"""恢复入口的抢占：一次中断只允许换一次执行。

为什么单独一组测试，而不是往 `test_checkpoint_resume.py` 里再加一条：那边测的是
"恢复之后状态对不对"，这一组测的是"**两个请求同时来，谁有权跑**"。后者的失效方式
不是报错，是**静默地跑第二遍**——而写操作不可逆，审批整套东西存在的理由就是
"一次同意换一次执行"。

关于测试边界，说清楚以免高估这里的东西：真正的危险来自**跨进程**（两个 worker
各收到一次"同意"，各自读到 `waiting_approval`）。单进程内的 asyncio 在读和写之间
没有 await，复现不出那个交错。所以这里分两层：

- 直接测 `claim_for_resume` 的谓词语义。这是跨进程时唯一仍然成立的东西：赢家只有
  一个，判据是同一条 UPDATE 在行锁上的串行，不是"读起来像原子"。
- 端到端测不变式：第一次恢复还在流式途中，第二次必须被拒、写入只发生一次。这条
  防的是以后有人把状态推进挪到某个 await 之后，把窗口重新打开。

`continue_orphan` 那一路额外测了"状态没变"的坑：从 ``running`` 接回来到处还是
``running``，条件 UPDATE 对它是无效的（两次都满足谓词、各赢一次）。那种情形只能
靠时间判，所以单独钉住。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from conftest import collect, run
from config import settings
from models import AgentRun
from services import checkpoint_store
from services.clock import naive_now

from tests.test_checkpoint_resume import enable_checkpoints, seed_admin
from tests.test_sse_contract import make_service
from tests.test_service_security import RecordingKnowledge


def _mk_run(
    db,
    *,
    status: str = "waiting_approval",
    updated_at: datetime | None = None,
    user_id: str = "u-1",
) -> str:
    """种一行指定状态的执行记录，返回 run_id。"""
    when = updated_at or naive_now()
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    db.add(
        AgentRun(
            id=run_id,
            chat_id="chat-1",
            user_id=user_id,
            message_id="m-1",
            status=status,
            rounds=1,
            started_at=when,
            updated_at=when,
        )
    )
    db.commit()
    return run_id


# ========== 机制：条件 UPDATE 的谓词 ==========


def test_只有一个请求能抢到现场(db_real, monkeypatch):
    """两个请求抢同一个 waiting_approval 的 run：第一个赢，第二个必须拿 0 行。

    旧写法是"先 SELECT 看状态、再单独 UPDATE"，两个进程可以双双通过检查。
    """
    enable_checkpoints(monkeypatch)
    run_id = _mk_run(db_real, status="waiting_approval")

    first, _ = checkpoint_store.claim_for_resume(
        db_real, run_id, from_statuses=("waiting_approval",)
    )
    second, second_status = checkpoint_store.claim_for_resume(
        db_real, run_id, from_statuses=("waiting_approval",)
    )

    assert first is True
    assert second is False
    assert db_real.query(AgentRun).filter(AgentRun.id == run_id).first().status == (
        "running"
    )
    # 输的那个不是只拿到 False：它要知道现在是什么状态，才能给用户一句人话
    assert second_status == "running"


def test_状态不对的抢不到(db_real, monkeypatch):
    """已经跑完的执行不能被"同意"按钮再拉起来。"""
    enable_checkpoints(monkeypatch)
    run_id = _mk_run(db_real, status="done")

    claimed, status = checkpoint_store.claim_for_resume(
        db_real, run_id, from_statuses=("waiting_approval",)
    )

    assert claimed is False
    assert status == "done"


def test_不存在的run抢不到也拿不到状态(db_real, monkeypatch):
    enable_checkpoints(monkeypatch)

    claimed, status = checkpoint_store.claim_for_resume(
        db_real, "run-not-there", from_statuses=("waiting_approval",)
    )

    assert claimed is False
    assert status is None


def test_检查点没开时不拦路(db_real, monkeypatch):
    """``AGENT_CHECKPOINT_ENABLED=False`` 时 ``update_run`` 本来就是空操作。

    抢占跟着退化成"永远允许"，否则加这道守卫反而会把一条原本能走的路径关掉。
    """
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", False)
    run_id = _mk_run(db_real, status="waiting_approval")

    claimed, _status = checkpoint_store.claim_for_resume(
        db_real, run_id, from_statuses=("waiting_approval",)
    )

    assert claimed is True


# ========== running 的坑：状态没变，只能靠时间 ==========


def test_僵尸running能被接走新鲜running不能(db_real, monkeypatch):
    """进程被杀时 run 停在 ``running``，和"正有人在跑"看起来一模一样。

    唯一的区别是多久没人碰过它。这个判据顺带把并发接续也挡住了：赢的那个把
    ``updated_at`` 推到现在，另一个的 ``< stale_before`` 当场不成立。
    """
    enable_checkpoints(monkeypatch)
    long_ago = naive_now() - timedelta(hours=2)
    zombie = _mk_run(db_real, status="running", updated_at=long_ago)
    live = _mk_run(db_real, status="running", user_id="u-2")  # updated_at = now

    stale_before = naive_now() - timedelta(minutes=13)

    claimed, _ = checkpoint_store.claim_for_resume(
        db_real, zombie, from_statuses=("interrupted",), stale_before=stale_before
    )
    refused, refused_status = checkpoint_store.claim_for_resume(
        db_real, live, from_statuses=("interrupted",), stale_before=stale_before
    )

    assert claimed is True
    assert refused is False
    assert refused_status == "running"


def test_interrupted不看时间(db_real, monkeypatch):
    """SSE 的 finally 明确标过 ``interrupted``，状态本身就是判据，不必等它变旧。"""
    enable_checkpoints(monkeypatch)
    run_id = _mk_run(db_real, status="interrupted")

    claimed, _ = checkpoint_store.claim_for_resume(
        db_real,
        run_id,
        from_statuses=("interrupted",),
        stale_before=naive_now() + timedelta(days=1),
    )

    assert claimed is True


# ========== 端到端不变式 ==========


def test_第一次恢复还在途中第二次不能接着跑(db_real, monkeypatch):
    """两个"同意"同时在场上，写操作只执行一次。

    这里让 A 跑到第一个事件（已经抢到现场、流还没结束），再让 B 完整走一遍。
    断言的不是 B 的报错文案，而是 ``uploaded`` 只有一条——那才是不可逆的部分。
    """
    enable_checkpoints(monkeypatch)
    admin_id = seed_admin(db_real)
    knowledge = RecordingKnowledge()
    service, _adapter = make_service(
        [
            {
                "tool_calls": [
                    ("save_to_knowledge_base", {"name": "要点", "content": "正文"})
                ]
            },
            {"text": "已保存。"},
        ],
        knowledge,
    )
    first = run(
        collect(
            service.stream_ai_response(
                db_real, admin_id, "c1", "存一下", use_rag=True, message_id="m-user"
            )
        )
    )
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    async def interleaved():
        a = service.resume_turn(db_real, admin_id, run_id, approved=True)
        a_first = await a.__anext__()  # A 抢到现场，流还开着
        b_events = [
            e async for e in service.resume_turn(db_real, admin_id, run_id, approved=True)
        ]
        a_rest = [e async for e in a]
        return a_first, b_events, a_rest

    a_first, b_events, _a_rest = run(interleaved())

    assert a_first["type"] != "error"
    assert b_events and b_events[0]["type"] == "error"
    assert len(knowledge.uploaded) == 1


def test_参数填错不该把审批消耗掉(db_real, monkeypatch):
    """校验早于抢占：一次写错的"改参数再同意"不该吃掉这张审批。

    反过来说，一旦开始执行就要把令牌花掉——那确实是执行过一次。
    """
    enable_checkpoints(monkeypatch)
    admin_id = seed_admin(db_real)
    knowledge = RecordingKnowledge()
    service, _adapter = make_service(
        [
            {
                "tool_calls": [
                    ("save_to_knowledge_base", {"name": "要点", "content": "正文"})
                ]
            },
            {"text": "已保存。"},
        ],
        knowledge,
    )
    first = run(
        collect(
            service.stream_ai_response(
                db_real, admin_id, "c1", "存一下", use_rag=True, message_id="m-user"
            )
        )
    )
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    bad = run(
        collect(
            service.resume_turn(
                db_real,
                admin_id,
                run_id,
                approved=True,
                edited_arguments={"name": "要点", "content": "正文", "多一个键": "x"},
            )
        )
    )
    assert bad[0]["type"] == "error"
    assert "参数修改无效" in bad[0]["error"]
    # 审批还在：同一张单子接着点一次正常的同意，应该真的执行
    ok = run(collect(service.resume_turn(db_real, admin_id, run_id, approved=True)))
    assert ok[0]["type"] != "error"
    assert len(knowledge.uploaded) == 1
