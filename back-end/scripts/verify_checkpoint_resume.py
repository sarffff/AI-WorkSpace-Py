"""手动验证 checkpoint / resume 全链路。不依赖 pytest，直接 python 跑。

    python scripts/verify_checkpoint_resume.py

为什么要有这个脚本（而不是只留 tests/test_checkpoint_resume.py）：中断恢复的失败
模式几乎全是**静默**的——工具悄悄跑了第二遍、预算悄悄回满、子代理的步骤悄悄挂到
了主代理名下。这些在真实使用里都不报错，只是账单变高、上限失效。所以需要一条能
一眼看完的输出，把每一步的实际状态打出来对照。

用真 SQLite（不是替身 Session）：这套逻辑的重点就是"状态真的落库、能从库里读回来"。
模型和知识库仍然是替身，脚本不触网。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from config import settings  # noqa: E402
from database import Base  # noqa: E402
import models  # noqa: E402,F401
from models import (  # noqa: E402
    AgentCheckpoint,
    AgentRun,
    Chat,
    Message,
    MessageToolStep,
    User,
    Workspace,
)
from services.chat_service import ChatService  # noqa: E402
from services.clock import naive_now  # noqa: E402
from services.model_adapter import (  # noqa: E402
    ModelAdapter,
    ModelCompletion,
    StreamChunk,
    ToolCall,
)
from services import checkpoint_store  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(label)
        print(f"  [OK]   {label}")
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))


class ScriptedAdapter(ModelAdapter):
    """逐轮回放模型行为，并记下每轮实际收到的 tools / messages。"""

    def __init__(self, rounds: list[dict[str, Any]]) -> None:
        self._rounds = list(rounds)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *, messages, tools, model, temperature=0.7,
                       max_tokens=2048, top_p=1.0, purpose=None) -> ModelCompletion:
        chunks = [
            chunk
            async for chunk in self.stream_completion(
                messages=messages, tools=tools, model=model
            )
        ]
        completion = chunks[-1].completion
        completion.streamed_length = 0
        return completion

    async def stream_completion(self, *, messages, tools, model, temperature=0.7,
                                max_tokens=2048, top_p=1.0
                                ) -> AsyncGenerator[StreamChunk, None]:
        self.calls.append(
            {
                "tools": [tool["function"]["name"] for tool in tools],
                "messages": [dict(message) for message in messages],
            }
        )
        assert self._rounds, "脚本已用尽：循环没有按预期终止"
        spec = self._rounds.pop(0)
        text = spec.get("text", "")
        streamed = 0
        if text:
            yield StreamChunk(text=text)
            streamed = len(text)
        calls = [
            ToolCall(
                id=f"call-{index}",
                name=name,
                arguments=json.dumps(arguments, ensure_ascii=False),
            )
            for index, (name, arguments) in enumerate(spec.get("tool_calls", []))
        ]
        yield StreamChunk(
            completion=ModelCompletion(
                content=text, tool_calls=calls, streamed_length=streamed
            )
        )


class FakeKnowledge:
    """知识库替身，记录写入调用。"""

    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []

    async def build_rag_context_with_citations(self, db, query, workspace_id, top_k=5):
        return "", []

    async def get_documents(self, db, workspace_id):
        return []

    async def read_chunks(self, db, workspace_id, document_id, chunk_index, window=1):
        return []

    async def upload_document(self, db, filename, content, workspace_id, uploader_id=None):
        self.uploaded.append((filename, workspace_id))
        return SimpleNamespace(id=f"doc-{len(self.uploaded)}", chunks=1)

    async def delete_document(self, db, document_id, workspace_id):
        return True


def fresh_db():
    """真建表的内存库，并种一个 admin 用户 + 一个会话。

    用户必须是真的：``save_to_knowledge_base`` 会查 ``users`` 决定
    ``scope.is_admin``，查不到就直接拒绝写入并返回一段说明——状态仍是 ok，
    于是"工具跑了但什么都没写"完全不报错。这正是验证脚本第一次跑出来的现象。
    """
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    now = naive_now()
    workspace = Workspace(id="w1", name="验证空间", created_at=now)
    session.add(workspace)
    session.add(
        User(
            id="u1",
            email="verify@example.com",
            username="verify",
            role="admin",
            workspace_id="w1",
        )
    )
    session.add(Chat(id="c1", user_id="u1", title="验证", created_at=now, updated_at=now))
    session.add(
        Message(id="m-user", chat_id="c1", role="user", content="问题", created_at=now)
    )
    session.commit()
    return session


def configure(*, approval_mode: str = "write", delegation: str = "off",
              delegations: int = 3, total_chars: int = 12000) -> None:
    settings.AGENT_CHECKPOINT_ENABLED = True
    settings.AGENT_CHECKPOINT_KEEP = 8
    settings.AGENT_APPROVAL_MODE = approval_mode
    settings.AGENT_APPROVAL_TOOLS = ""
    settings.AGENT_DELEGATION_MODE = delegation
    settings.AGENT_MAX_DELEGATIONS = delegations
    settings.TOOL_RESULT_TOTAL_CHARS = total_chars
    settings.RAG_PREFETCH = False
    settings.TELEMETRY_ENABLED = False
    settings.MEMORY_ENABLED = False
    settings.TOOL_CALCULATE_ENABLED = True
    settings.TOOL_WRITE_KNOWLEDGE_ENABLED = True
    settings.TOOL_DELETE_KNOWLEDGE_ENABLED = False
    settings.TOOL_WEB_SEARCH_ENABLED = False
    settings.TOOL_READ_ATTACHMENT_ENABLED = False
    settings.TOOL_ASK_USER_ENABLED = False
    settings.TOOL_WEB_FETCH_ENABLED = False
    settings.SEMANTIC_CACHE_ENABLED = False


def build(rounds: list[dict[str, Any]]) -> tuple[ChatService, ScriptedAdapter, FakeKnowledge]:
    adapter = ScriptedAdapter(rounds)
    knowledge = FakeKnowledge()
    service = ChatService(model_adapter=adapter)
    service._knowledge_service = knowledge  # type: ignore[assignment]
    return service, adapter, knowledge


async def collect(agen) -> list[dict[str, Any]]:
    return [event async for event in agen]


async def stream(service, db, prompt: str = "请处理") -> list[dict[str, Any]]:
    return await collect(
        service.stream_ai_response(
            db, "u1", "c1", prompt, use_rag=False, message_id="m-user"
        )
    )


def tool_messages(adapter: ScriptedAdapter) -> list[str]:
    return [
        message["content"]
        for message in adapter.calls[-1]["messages"]
        if message.get("role") == "tool"
    ]


# ========== 场景 1：中断点 ==========


async def scenario_interrupt_point() -> None:
    print("\n[1] 写操作在执行之前中断，状态落库")
    configure()
    db = fresh_db()
    service, _adapter, knowledge = build(
        [
            {"tool_calls": [("save_to_knowledge_base", {"name": "要点", "content": "正文"})]},
            {"text": "已保存。"},
        ]
    )
    events = await stream(service, db)
    kinds = [event["type"] for event in events]

    check("发出 approval_required", "approval_required" in kinds)
    check("中断在 tool_start 之前（整条流没有 tool_start）", "tool_start" not in kinds)
    check("工具没有真的执行", knowledge.uploaded == [])

    request = next(event for event in events if event["type"] == "approval_required")
    check("预览带上了参数", request["preview"].get("name") == "要点")
    check("给出了审批理由", bool(request["reason"]))

    row = db.query(AgentRun).filter(AgentRun.id == request["runId"]).first()
    check("run 落在 waiting_approval", row is not None and row.status == "waiting_approval",
          f"status={getattr(row, 'status', None)}")
    check("interrupts 计数为 1", row is not None and row.interrupts == 1)
    snapshot = (
        db.query(AgentCheckpoint)
        .filter(AgentCheckpoint.run_id == request["runId"])
        .order_by(AgentCheckpoint.seq.desc())
        .first()
    )
    check("快照 phase = waiting_approval", snapshot is not None and snapshot.phase == "waiting_approval")
    check("快照带着中断请求",
          snapshot is not None and json.loads(snapshot.interrupt)["tool"] == "save_to_knowledge_base")
    print(f"       事件序列: {' -> '.join(kinds)}")
    db.close()


# ========== 场景 2：批准后恢复 ==========


async def scenario_resume_approved() -> None:
    print("\n[2] 批准后恢复：工具真的执行，回合跑完")
    configure()
    db = fresh_db()
    service, _adapter, knowledge = build(
        [
            {"tool_calls": [("save_to_knowledge_base", {"name": "要点", "content": "正文"})]},
            {"text": "已保存。"},
        ]
    )
    first = await stream(service, db)
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    resumed = await collect(service.resume_turn(db, "u1", run_id, approved=True))
    kinds = [event["type"] for event in resumed]

    check("第一个事件是 approval_resolved", kinds and kinds[0] == "approval_resolved")
    check("resolved 标记为已批准", resumed[0].get("approved") is True)
    check("这一次工具真的执行了", "tool_start" in kinds and "tool_result" in kinds)
    result = next((e for e in resumed if e["type"] == "tool_result"), None)
    check("工具返回 ok", result is not None and result["status"] == "ok")
    check("写入确实发生了一次", len(knowledge.uploaded) == 1, f"uploaded={knowledge.uploaded}")
    answer = "".join(e["content"] for e in resumed if e["type"] == "message_delta")
    check("恢复后流出了最终回答", answer == "已保存。", f"answer={answer!r}")

    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    check("run 落在 done", row.status == "done", f"status={row.status}")
    check("finished_at 已填", row.finished_at is not None)
    print(f"       事件序列: {' -> '.join(kinds)}")
    db.close()


# ========== 场景 3：拒绝 ==========


async def scenario_resume_rejected() -> None:
    print("\n[3] 拒绝后恢复：工具没跑，拒绝理由回灌给模型")
    configure()
    db = fresh_db()
    service, adapter, knowledge = build(
        [
            {"tool_calls": [("save_to_knowledge_base", {"name": "要点", "content": "正文"})]},
            {"text": "好的，那我先不保存。"},
        ]
    )
    first = await stream(service, db)
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    resumed = await collect(
        service.resume_turn(db, "u1", run_id, approved=False, note="先别写库")
    )

    check("resolved 标记为已拒绝", resumed[0].get("approved") is False)
    result = next((e for e in resumed if e["type"] == "tool_result"), None)
    check("工具结果状态是 rejected", result is not None and result["status"] == "rejected",
          f"status={getattr(result, 'get', lambda _: None)('status') if result else None}")
    check("工具一次都没有真的执行", knowledge.uploaded == [])
    messages = tool_messages(adapter)
    check("回灌内容里说明了被拒绝", any("拒绝" in text for text in messages))
    check("回灌内容里带上了用户备注", any("先别写库" in text for text in messages))

    step = (
        db.query(MessageToolStep)
        .filter(MessageToolStep.tool_name == "save_to_knowledge_base")
        .first()
    )
    check("轨迹落库状态是 rejected", step is not None and step.status == "rejected")
    check("轨迹挂在正确的 run 上", step is not None and step.run_id == run_id)
    row = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    check("拒绝之后回合仍然正常收尾", row.status == "done", f"status={row.status}")
    db.close()


# ========== 场景 4：幂等 ==========


async def scenario_idempotency() -> None:
    print("\n[4] 幂等：恢复后已执行的工具不重跑，结果被摆回上下文")
    configure()
    db = fresh_db()
    service, adapter, knowledge = build(
        [
            {
                "tool_calls": [
                    ("calculate", {"expression": "6*7"}),
                    ("save_to_knowledge_base", {"name": "结果", "content": "42"}),
                ]
            },
            {"text": "都办好了。"},
        ]
    )
    first = await stream(service, db)
    first_tools = [e["tool"] for e in first if e["type"] == "tool_result"]
    check("中断前只跑了 calculate", first_tools == ["calculate"], f"tools={first_tools}")
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    state = checkpoint_store.latest(db, run_id)
    check("快照里记了一条 write", len(state.writes) == 1)
    check("write 记的是 calculate", state.writes and state.writes[0]["name"] == "calculate")
    check("pending_index 指向未执行的那个", state.pending_index == 1, f"index={state.pending_index}")

    resumed = await collect(service.resume_turn(db, "u1", run_id, approved=True))
    resumed_tools = [e["tool"] for e in resumed if e["type"] == "tool_result"]
    check("恢复段只执行了写操作", resumed_tools == ["save_to_knowledge_base"],
          f"tools={resumed_tools}")

    calc_steps = (
        db.query(MessageToolStep).filter(MessageToolStep.tool_name == "calculate").all()
    )
    check("calculate 在库里只有一条轨迹（没重跑）", len(calc_steps) == 1,
          f"count={len(calc_steps)}")
    messages = tool_messages(adapter)
    check("模型上下文里有两条工具结果", len(messages) == 2, f"count={len(messages)}")
    check("被 replay 摆回的结果内容还在", any("42" in text for text in messages))
    db.close()


# ========== 场景 5：重复裁决 ==========


async def scenario_double_resume() -> None:
    print("\n[5] 同一审批点两次：第二次被挡住，写操作不会执行两遍")
    configure()
    db = fresh_db()
    service, _adapter, knowledge = build(
        [
            {"tool_calls": [("save_to_knowledge_base", {"name": "要点", "content": "正文"})]},
            {"text": "已保存。"},
        ]
    )
    first = await stream(service, db)
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    await collect(service.resume_turn(db, "u1", run_id, approved=True))
    second = await collect(service.resume_turn(db, "u1", run_id, approved=True))

    check("第二次恢复返回错误", second and second[0]["type"] == "error")
    check("错误说明了当前状态", second and "不在等待审批" in second[0].get("error", ""))
    check("写入仍然只有一次", len(knowledge.uploaded) == 1, f"uploaded={len(knowledge.uploaded)}")

    other = await collect(service.resume_turn(db, "attacker", run_id, approved=True))
    check("别人的 run 不能恢复", other and other[0]["type"] == "error")
    check("越权错误信息正确", other and "不属于当前用户" in other[0].get("error", ""))
    db.close()


# ========== 场景 6：余额不因中断重置 ==========


async def scenario_budget_survives() -> None:
    print("\n[6] 中断不重置任何上限：预算 / 重复计数 / 熔断状态都拨回中断那一刻")
    configure(total_chars=400)
    db = fresh_db()
    service, _adapter, _knowledge = build(
        [
            {
                "tool_calls": [
                    ("calculate", {"expression": "1+1"}),
                    ("save_to_knowledge_base", {"name": "n", "content": "c"}),
                ]
            },
            {"text": "完成。"},
        ]
    )
    first = await stream(service, db)
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    state = checkpoint_store.latest(db, run_id)
    check("预算已经扣过（不是初始值）", state.budget_remaining < 400,
          f"remaining={state.budget_remaining} total=400")
    check("重复计数进了快照", bool(state.repeat_counts), f"counts={state.repeat_counts}")
    check("write 里带着当时的余额快照",
          state.writes and "budget_after" in state.writes[0])
    check("write 里带着守卫快照",
          state.writes and "repeat_counts" in state.writes[0]["guards"])

    before = state.budget_remaining
    await collect(service.resume_turn(db, "u1", run_id, approved=True))
    after = checkpoint_store.latest(db, run_id)
    check("恢复后余额没有回弹", after.budget_remaining <= before,
          f"before={before} after={after.budget_remaining}")
    db.close()


# ========== 场景 7：多代理归属 ==========


async def scenario_subagent_runs() -> None:
    print("\n[7] 多代理：子代理各自一行 agent_runs，工具步骤挂到子 run 上")
    configure(approval_mode="off", delegation="augment")
    db = fresh_db()
    service, _adapter, _knowledge = build(
        [
            {"tool_calls": [("delegate", {"role": "analyst", "task": "算一下 6*7"})]},
            {"tool_calls": [("calculate", {"expression": "6*7"})]},
            {"text": "报告：42"},
            {"text": "最终回答：42。"},
        ]
    )
    events = await stream(service, db)

    parents = db.query(AgentRun).filter(AgentRun.parent_run_id.is_(None)).all()
    check("主代理一行 run", len(parents) == 1, f"count={len(parents)}")
    parent = parents[0]
    children = db.query(AgentRun).filter(AgentRun.parent_run_id == parent.id).all()
    check("子代理一行 run", len(children) == 1, f"count={len(children)}")
    if children:
        child = children[0]
        check("子 run 角色正确", child.agent_role == "analyst", f"role={child.agent_role}")
        check("子 run 落终态", child.status == "done", f"status={child.status}")
        check("子 run 记了轮次", child.rounds >= 1, f"rounds={child.rounds}")

        calc = db.query(MessageToolStep).filter(
            MessageToolStep.tool_name == "calculate"
        ).all()
        check("calculate 挂在子 run 上",
              len(calc) == 1 and calc[0].run_id == child.id,
              f"run_id={calc[0].run_id if calc else None} child={child.id}")
        delegate = db.query(MessageToolStep).filter(
            MessageToolStep.tool_name == "delegate"
        ).all()
        check("delegate 挂在父 run 上",
              len(delegate) == 1 and delegate[0].run_id == parent.id)
        check("delegate 的 agent_role 为空（主代理自己调的）",
              len(delegate) == 1 and delegate[0].agent_role is None)
        completed = next(
            (e for e in events
             if e["type"] == "agent_state" and e.get("status") == "completed"),
            None,
        )
        check("agent_state 事件带上了子 run id",
              completed is not None and completed.get("runId") == child.id)
    db.close()


async def scenario_delegation_cap_survives() -> None:
    print("\n[8] 委派上限跨中断有效：恢复后不会重新拿到满额委派次数")
    configure(approval_mode="write", delegation="augment", delegations=1)
    db = fresh_db()
    service, adapter, _knowledge = build(
        [
            {
                "tool_calls": [
                    ("delegate", {"role": "analyst", "task": "算 1+1"}),
                    ("save_to_knowledge_base", {"name": "n", "content": "c"}),
                ]
            },
            {"text": "子代理报告：2"},
            {"tool_calls": [("delegate", {"role": "analyst", "task": "再算一次"})]},
            {"text": "最终回答。"},
        ]
    )
    first = await stream(service, db)
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    state = checkpoint_store.latest(db, run_id)
    check("委派次数进了快照", state.delegations_used == 1,
          f"used={state.delegations_used}")

    await collect(service.resume_turn(db, "u1", run_id, approved=True))
    messages = tool_messages(adapter)
    check("恢复后的委派被上限挡住",
          any("委派次数已达上限" in text for text in messages),
          f"messages={[text[:40] for text in messages]}")
    parent = db.query(AgentRun).filter(AgentRun.parent_run_id.is_(None)).first()
    children = db.query(AgentRun).filter(AgentRun.parent_run_id == parent.id).all()
    check("没有产生第二个子 run", len(children) == 1, f"count={len(children)}")
    db.close()


# ========== 场景 9：状态往返 ==========


async def scenario_state_roundtrip() -> None:
    print("\n[9] 快照序列化往返无损，且容忍未知字段")
    from services.agent_state import TurnState, make_write, replay_writes

    original = TurnState(
        run_id="r1", chat_id="c1", user_id="u1", workspace_id="w1",
        messages=[{"role": "user", "content": "问题"}],
        round_index=3, budget_remaining=1234,
        repeat_counts={"calculate\x001+1": 2},
        breaker_tripped=["web_search"], delegations_used=2,
        approved_call_ids=["call-0"],
    )
    revived = TurnState.from_json(original.to_json())
    check("往返之后完全相等", revived == original)

    payload = json.loads(original.to_json())
    payload["some_future_field"] = 1
    tolerant = TurnState.from_json(json.dumps(payload))
    check("未知字段被忽略而不是抛异常", tolerant.run_id == "r1" and tolerant.round_index == 3)

    # replay 只拨余额与游标，**不**重新追加消息：结果早就在快照的 messages 里了
    # （循环是直接往那个列表追加的）。再追加一遍会让模型看到同一结果两次。
    state = TurnState(
        run_id="r2", chat_id="c1", user_id="u1", workspace_id="w1",
        messages=[
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "call-0", "content": "42"},
            {"role": "tool", "tool_call_id": "call-1", "content": "失败"},
        ],
        budget_remaining=9999,
    )
    state.writes = [
        make_write(index=0, call_id="call-0", name="calculate", status="ok",
                   content="42", budget_after=880, repeat_counts={"calculate\x00{}": 1},
                   repeat_blocked=0, breaker_consecutive={}, breaker_tripped=[]),
        make_write(index=1, call_id="call-1", name="web_search", status="unavailable",
                   content="失败", budget_after=760, repeat_counts={"calculate\x00{}": 1},
                   repeat_blocked=0, breaker_consecutive={"web_search": 1},
                   breaker_tripped=["web_search"]),
    ]
    before_messages = len(state.messages)
    replay_writes(state)
    check("replay 不重复追加消息（结果已在快照里）",
          len(state.messages) == before_messages,
          f"before={before_messages} after={len(state.messages)}")
    check("replay 把余额拨到最后一条 write", state.budget_remaining == 760,
          f"remaining={state.budget_remaining}")
    check("replay 恢复了熔断状态", state.breaker_tripped == ["web_search"])
    check("replay 把游标挪到已完成数", state.pending_index == 2)


async def main() -> int:
    print("=" * 72)
    print("checkpoint / resume 全链路验证（真 SQLite，模型与知识库为替身）")
    print("=" * 72)
    await scenario_interrupt_point()
    await scenario_resume_approved()
    await scenario_resume_rejected()
    await scenario_idempotency()
    await scenario_double_resume()
    await scenario_budget_survives()
    await scenario_subagent_runs()
    await scenario_delegation_cap_survives()
    await scenario_state_roundtrip()

    print("\n" + "=" * 72)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for label in FAIL:
            print(f"  FAILED: {label}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
