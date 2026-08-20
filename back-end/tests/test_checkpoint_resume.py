"""状态快照与中断恢复（F）：审批、幂等、余额、多代理归属。

这一组测的不是"审批弹窗能不能弹出来"，而是四件真正会静默错掉的事：

1. **中断点的位置。** 停在 ``tool_start`` **之前**。停晚了界面上会留一个永远
   等不到 ``tool_result`` 的"正在执行"，而那个工具一步都没跑。
2. **幂等。** 恢复之后本轮前面已经执行过的工具**不能再跑一遍**。它们花过的
   检索成本是真的，重跑一次不会得到新结果，只会付第二次钱。
3. **余额。** 预算、重复计数、熔断状态都要拨回中断那一刻。不拨的话，
   "中断一次"就等于"把所有上限重置一次"——用户点一下同意，模型又拿到一整份预算。
4. **多代理归属。** 子代理各自一行 ``agent_runs``，``parent_run_id`` 指向主代理。
   靠 ``(agent_role, 时间)`` 去推断的话，一回合里委派两个同角色子代理必然错。

全部用真 SQLite（``db_real``）而不是 ``FakeDB``：这套逻辑的重点就是"状态真的
落到库里了、能从库里读回来"，用空操作的替身全测不出来。
"""
from __future__ import annotations

import json
from typing import Any

from conftest import collect, run
from config import settings
from models import AgentCheckpoint, AgentRun, MessageToolStep

from tests.test_sse_contract import make_service, enable_delegation
from tests.test_service_security import RecordingKnowledge, _seed_workspace


def seed_admin(db_real) -> str:
    """种一个 admin 用户 + 工作区，返回它的 id。

    写操作必须用真用户：``save_to_knowledge_base`` 会查 ``users`` 决定
    ``scope.is_admin``，查不到就拒绝写入并返回一段说明——而状态仍是 ``ok``。
    于是"工具跑了但什么都没写"完全不报错，断言 ``uploaded == []`` 会假通过。
    """
    admin_id, _member_id = _seed_workspace(db_real)
    return admin_id


def enable_checkpoints(monkeypatch, *, approval_mode: str = "write") -> None:
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_KEEP", 8)
    monkeypatch.setattr(settings, "AGENT_APPROVAL_MODE", approval_mode)
    monkeypatch.setattr(settings, "AGENT_APPROVAL_TOOLS", "")
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "TOOL_CALCULATE_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_WRITE_KNOWLEDGE_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_WEB_SEARCH_ENABLED", False)
    monkeypatch.setattr(settings, "TOOL_READ_ATTACHMENT_ENABLED", False)
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "off")


def types_of(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


# ========== 中断点 ==========


def test_write_tool_interrupts_before_execution(db_real, monkeypatch):
    """写操作在执行**之前**停下：没有 tool_start，工具没跑，状态落了库。"""
    enable_checkpoints(monkeypatch)
    admin_id = seed_admin(db_real)
    knowledge = RecordingKnowledge()
    service, _adapter = make_service(
        [
            {
                "tool_calls": [
                    ("save_to_knowledge_base", {"name": "会议要点", "content": "正文"})
                ]
            },
            {"text": "已保存。"},
        ],
        knowledge,
    )

    events = run(
        collect(
            service.stream_ai_response(
                db_real, admin_id, "c1", "把这段存进知识库", use_rag=True, message_id="m-user"
            )
        )
    )

    kinds = types_of(events)
    assert "approval_required" in kinds
    # 停在 tool_start 之前：整条流里一个 tool_start 都没有
    assert "tool_start" not in kinds
    assert "tool_result" not in kinds
    # 真实写入从未发生
    assert knowledge.uploaded == []

    request = next(event for event in events if event["type"] == "approval_required")
    assert request["tool"] == "save_to_knowledge_base"
    assert request["preview"]["name"] == "会议要点"
    assert request["reason"]

    run_row = db_real.query(AgentRun).filter(AgentRun.id == request["runId"]).first()
    assert run_row is not None
    assert run_row.status == "waiting_approval"
    assert run_row.interrupts == 1
    # 快照真的落库了，而且带着中断请求
    snapshot = (
        db_real.query(AgentCheckpoint)
        .filter(AgentCheckpoint.run_id == request["runId"])
        .order_by(AgentCheckpoint.seq.desc())
        .first()
    )
    assert snapshot.phase == "waiting_approval"
    assert json.loads(snapshot.interrupt)["tool"] == "save_to_knowledge_base"


def test_non_gated_tool_runs_without_interruption(db_real, monkeypatch):
    """只有闸门里的工具才停。calculate 照常一路跑完，行为与开关关闭时一致。"""
    enable_checkpoints(monkeypatch)
    admin_id = seed_admin(db_real)
    service, _adapter = make_service(
        [
            {"tool_calls": [("calculate", {"expression": "2+2"})]},
            {"text": "等于 4。"},
        ]
    )

    events = run(
        collect(
            service.stream_ai_response(
                db_real, admin_id, "c1", "算一下", use_rag=False, message_id="m-user"
            )
        )
    )

    kinds = types_of(events)
    assert "approval_required" not in kinds
    assert "tool_start" in kinds and "tool_result" in kinds
    assert "".join(
        event["content"] for event in events if event["type"] == "message_delta"
    ).endswith("等于 4。")


# ========== 批准后恢复 ==========


def test_resume_approved_executes_and_finishes(db_real, monkeypatch):
    """批准之后：工具真的执行、回合跑完、run 落终态。"""
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

    resumed = run(
        collect(service.resume_turn(db_real, admin_id, run_id, approved=True))
    )

    kinds = types_of(resumed)
    assert kinds[0] == "approval_resolved"
    assert resumed[0]["approved"] is True
    # 这一次工具真的跑了
    assert "tool_start" in kinds and "tool_result" in kinds
    result = next(e for e in resumed if e["type"] == "tool_result")
    assert result["status"] == "ok"
    assert len(knowledge.uploaded) == 1
    assert "".join(
        e["content"] for e in resumed if e["type"] == "message_delta"
    ) == "已保存。"

    run_row = db_real.query(AgentRun).filter(AgentRun.id == run_id).first()
    assert run_row.status == "done"
    assert run_row.finished_at is not None


def test_resume_rejected_feeds_reason_back_to_model(db_real, monkeypatch):
    """拒绝之后：工具没跑，但模型收到一条"用户拒绝了"的工具结果，据此改口。"""
    enable_checkpoints(monkeypatch)
    admin_id = seed_admin(db_real)
    knowledge = RecordingKnowledge()
    service, adapter = make_service(
        [
            {
                "tool_calls": [
                    ("save_to_knowledge_base", {"name": "要点", "content": "正文"})
                ]
            },
            {"text": "好的，那我不保存，先给你看内容。"},
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

    resumed = run(
        collect(
            service.resume_turn(
                db_real, admin_id, run_id, approved=False, note="先别写库"
            )
        )
    )

    assert resumed[0]["type"] == "approval_resolved"
    assert resumed[0]["approved"] is False
    result = next(e for e in resumed if e["type"] == "tool_result")
    assert result["status"] == "rejected"
    # 工具一次都没有真的执行
    assert knowledge.uploaded == []
    # 回灌给模型的那条结果里说清了"被拒绝"和用户的补充说明
    tool_messages = [
        message for message in adapter.calls[-1]["messages"] if message["role"] == "tool"
    ]
    assert "拒绝" in tool_messages[-1]["content"]
    assert "先别写库" in tool_messages[-1]["content"]
    # 落库的轨迹也是 rejected：下个回合模型才知道这条路被否过
    step = (
        db_real.query(MessageToolStep)
        .filter(MessageToolStep.tool_name == "save_to_knowledge_base")
        .first()
    )
    assert step.status == "rejected"
    assert step.run_id == run_id


# ========== 幂等：已执行的工具不重跑 ==========


def test_resume_does_not_reexecute_completed_calls(db_real, monkeypatch):
    """同一轮里 calculate 先跑、写操作被拦下。恢复之后 calculate **不再跑**，
    它当时的结果被摆回上下文。

    这是幂等性的核心。重跑一遍的话，那次检索/计算的成本会付第二遍，而结果
    不会更新——更糟的是如果那一步有副作用（发消息、扣款），副作用会发生两次。
    """
    enable_checkpoints(monkeypatch)
    admin_id = seed_admin(db_real)
    knowledge = RecordingKnowledge()
    service, adapter = make_service(
        [
            {
                "tool_calls": [
                    ("calculate", {"expression": "6*7"}),
                    ("save_to_knowledge_base", {"name": "结果", "content": "42"}),
                ]
            },
            {"text": "都办好了。"},
        ],
        knowledge,
    )
    first = run(
        collect(
            service.stream_ai_response(
                db_real, admin_id, "c1", "算完存起来", use_rag=True, message_id="m-user"
            )
        )
    )
    # 第一段：calculate 跑完了，写操作把回合停住
    first_results = [e for e in first if e["type"] == "tool_result"]
    assert [e["tool"] for e in first_results] == ["calculate"]
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    resumed = run(collect(service.resume_turn(db_real, admin_id, run_id, approved=True)))

    # 恢复段只执行了写操作，calculate 没有第二次
    resumed_results = [e["tool"] for e in resumed if e["type"] == "tool_result"]
    assert resumed_results == ["save_to_knowledge_base"]
    # 数据库里 calculate 也只有一条轨迹
    calc_steps = (
        db_real.query(MessageToolStep)
        .filter(MessageToolStep.tool_name == "calculate")
        .all()
    )
    assert len(calc_steps) == 1
    # 而模型看到的上下文里，calculate 的结果确实在（被 replay 摆回去了）
    tool_messages = [
        message for message in adapter.calls[-1]["messages"] if message["role"] == "tool"
    ]
    assert len(tool_messages) == 2
    assert "42" in tool_messages[0]["content"]


def test_resume_twice_is_rejected(db_real, monkeypatch):
    """同一个审批被点两次（双击、两个标签页）：第二次必须被挡住。

    不挡的话已批准的写操作会执行第二遍——而它是不可逆的。
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

    run(collect(service.resume_turn(db_real, admin_id, run_id, approved=True)))
    second = run(collect(service.resume_turn(db_real, admin_id, run_id, approved=True)))

    assert second[0]["type"] == "error"
    assert "不在等待审批" in second[0]["error"]
    # 写入仍然只有一次
    assert len(knowledge.uploaded) == 1


def test_resume_rejects_other_users_run(db_real, monkeypatch):
    """别人的 run 不能恢复。审批是授权动作，越权恢复等于绕过整套授权。"""
    enable_checkpoints(monkeypatch)
    admin_id = seed_admin(db_real)
    service, _adapter = make_service(
        [
            {
                "tool_calls": [
                    ("save_to_knowledge_base", {"name": "x", "content": "y"})
                ]
            },
            {"text": "done"},
        ]
    )
    first = run(
        collect(
            service.stream_ai_response(
                db_real, admin_id, "c1", "存一下", use_rag=True, message_id="m-user"
            )
        )
    )
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    events = run(collect(service.resume_turn(db_real, "attacker", run_id, approved=True)))
    assert events[0]["type"] == "error"
    assert "不属于当前用户" in events[0]["error"]


# ========== 余额：中断不该重置任何上限 ==========


def test_budget_and_repeat_counts_survive_interrupt(db_real, monkeypatch):
    """恢复之后预算余额与重复计数拨回中断那一刻，而不是回到满额。

    不拨的话"中断一次"就是"把所有上限重置一次"：用户点一下同意，模型又能
    拿同样的参数再检索三遍，还多拿一整份字符预算。
    """
    enable_checkpoints(monkeypatch)
    monkeypatch.setattr(settings, "TOOL_RESULT_TOTAL_CHARS", 400)
    monkeypatch.setattr(settings, "TOOL_RESULT_MAX_CHARS", 200)
    admin_id = seed_admin(db_real)
    knowledge = RecordingKnowledge()
    service, _adapter = make_service(
        [
            {
                "tool_calls": [
                    ("calculate", {"expression": "1+1"}),
                    ("save_to_knowledge_base", {"name": "n", "content": "c"}),
                ]
            },
            {"text": "完成。"},
        ],
        knowledge,
    )
    first = run(
        collect(
            service.stream_ai_response(
                db_real, admin_id, "c1", "算完存", use_rag=True, message_id="m-user"
            )
        )
    )
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    from services import checkpoint_store

    state = checkpoint_store.latest(db_real, run_id)
    # 快照里的余额已经扣过 calculate 那一步，不是初始值
    assert state.budget_remaining < 400
    assert state.writes and state.writes[0]["name"] == "calculate"
    assert state.pending_index == 1
    # 重复计数也进了快照
    assert state.repeat_counts

    spent_before = state.budget_remaining
    run(collect(service.resume_turn(db_real, admin_id, run_id, approved=True)))
    after = checkpoint_store.latest(db_real, run_id)
    # 恢复之后余额从中断点继续往下扣，绝不回弹
    assert after.budget_remaining <= spent_before


def test_state_roundtrip_is_lossless(db_real, monkeypatch):
    """快照序列化 -> 反序列化不丢字段，且未知字段被忽略而不是炸掉。

    后者让"给 TurnState 加字段"不需要迁移历史快照——旧快照缺的键取默认值，
    新代码读旧快照照样能恢复。
    """
    from services.agent_state import TurnState

    original = TurnState(
        run_id="r1",
        chat_id="c1",
        user_id="u1",
        workspace_id="w1",
        messages=[{"role": "user", "content": "问题"}],
        round_index=3,
        budget_remaining=1234,
        repeat_counts={"calculate\x001+1": 2},
        breaker_tripped=["web_search"],
        delegations_used=2,
        approved_call_ids=["call-0"],
    )
    revived = TurnState.from_json(original.to_json())
    assert revived == original

    # 多一个未来字段：忽略，不抛
    payload = json.loads(original.to_json())
    payload["some_future_field"] = 1
    tolerant = TurnState.from_json(json.dumps(payload))
    assert tolerant.run_id == "r1"
    assert tolerant.round_index == 3


def test_replay_restores_balances_without_duplicating_messages():
    """``replay_writes`` 只拨余额与游标，**不**重新追加消息。

    快照是在工具循环进行中拍的，而循环直接往 ``state.messages`` 追加 ``role=tool``
    消息——已完成调用的结果本来就在快照里。按 writes 再追加一遍，模型会看到同一个
    工具结果两次，而两次之间没有任何矛盾迹象：它只会当成"查了两轮都是这个答案"。
    """
    from services.agent_state import TurnState, make_write, replay_writes

    state = TurnState(
        run_id="r2",
        chat_id="c1",
        user_id="u1",
        workspace_id="w1",
        messages=[
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "call-0", "content": "42"},
            {"role": "tool", "tool_call_id": "call-1", "content": "失败"},
        ],
        budget_remaining=9999,
    )
    state.writes = [
        make_write(
            index=0, call_id="call-0", name="calculate", status="ok", content="42",
            budget_after=880, repeat_counts={"calculate\x00{}": 1}, repeat_blocked=0,
            breaker_consecutive={}, breaker_tripped=[],
        ),
        make_write(
            index=1, call_id="call-1", name="web_search", status="unavailable",
            content="失败", budget_after=760, repeat_counts={"calculate\x00{}": 1},
            repeat_blocked=0, breaker_consecutive={"web_search": 1},
            breaker_tripped=["web_search"],
        ),
    ]
    before = len(state.messages)

    replay_writes(state)

    assert len(state.messages) == before
    # 余额与守卫状态取自**最后一条** write，也就是中断那一刻的样子
    assert state.budget_remaining == 760
    assert state.breaker_tripped == ["web_search"]
    assert state.breaker_consecutive == {"web_search": 1}
    assert state.pending_index == 2


# ========== 多代理：归属与隔离 ==========


def test_subagent_gets_its_own_run_row(db_real, monkeypatch):
    """每次委派一行 agent_runs，parent_run_id 指向主代理，工具步骤挂在子 run 上。

    这是"这次回答起了几个子代理、哪个失败了"从推断变成一次 SQL 查询的地方。
    靠 ``(agent_role, 时间)`` 推断的话，一回合里委派两个 researcher 就会错。
    """
    enable_checkpoints(monkeypatch, approval_mode="off")
    enable_delegation(monkeypatch, mode="augment")
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_CALCULATE_ENABLED", True)
    admin_id = seed_admin(db_real)
    service, _adapter = make_service(
        [
            {"tool_calls": [("delegate", {"role": "analyst", "task": "算一下 6*7"})]},
            {"tool_calls": [("calculate", {"expression": "6*7"})]},
            {"text": "结果是 42。"},
            {"text": "最终回答：42。"},
        ]
    )

    events = run(
        collect(
            service.stream_ai_response(
                db_real, admin_id, "c1", "算算", use_rag=False, message_id="m-user"
            )
        )
    )

    parent = (
        db_real.query(AgentRun)
        .filter(AgentRun.parent_run_id.is_(None))
        .one()
    )
    children = db_real.query(AgentRun).filter(AgentRun.parent_run_id == parent.id).all()
    assert len(children) == 1
    child = children[0]
    assert child.agent_role == "analyst"
    assert child.status == "done"
    assert child.rounds >= 1
    # 子代理跑的那一步挂在子 run 上，主代理的 delegate 挂在父 run 上
    calc_step = (
        db_real.query(MessageToolStep)
        .filter(MessageToolStep.tool_name == "calculate")
        .one()
    )
    assert calc_step.run_id == child.id
    assert calc_step.agent_role == "analyst"
    delegate_step = (
        db_real.query(MessageToolStep)
        .filter(MessageToolStep.tool_name == "delegate")
        .one()
    )
    assert delegate_step.run_id == parent.id
    assert delegate_step.agent_role is None
    # SSE 里也带上了子 run id，前端据此能点进去看这次委派的详情
    state_event = next(
        e for e in events if e["type"] == "agent_state" and e["status"] == "completed"
    )
    assert state_event["runId"] == child.id


def test_delegation_count_survives_interrupt(db_real, monkeypatch):
    """委派次数上限跨中断有效：恢复之后不会重新拿到满额的委派次数。

    不守住的话，"委派上限 1 次"会变成"每次审批之后再送 1 次"——一个愿意点
    同意的用户可以把成本推到没有上界。
    """
    enable_checkpoints(monkeypatch)
    enable_delegation(monkeypatch, mode="augment", limit=1)
    monkeypatch.setattr(settings, "AGENT_CHECKPOINT_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_APPROVAL_MODE", "write")
    monkeypatch.setattr(settings, "TOOL_WRITE_KNOWLEDGE_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_CALCULATE_ENABLED", True)
    admin_id = seed_admin(db_real)
    knowledge = RecordingKnowledge()
    service, adapter = make_service(
        [
            # 第 1 轮：委派一次（用掉唯一的额度），同一轮里再要求写入 -> 被拦
            {
                "tool_calls": [
                    ("delegate", {"role": "analyst", "task": "算 1+1"}),
                    ("save_to_knowledge_base", {"name": "n", "content": "c"}),
                ]
            },
            {"text": "子代理报告：2"},
            # 恢复后：再试一次委派，应该被上限挡住
            {"tool_calls": [("delegate", {"role": "analyst", "task": "再算一次"})]},
            {"text": "最终回答。"},
        ],
        knowledge,
    )
    first = run(
        collect(
            service.stream_ai_response(
                db_real, admin_id, "c1", "委派并保存", use_rag=False, message_id="m-user"
            )
        )
    )
    run_id = next(e for e in first if e["type"] == "approval_required")["runId"]

    from services import checkpoint_store

    state = checkpoint_store.latest(db_real, run_id)
    assert state.delegations_used == 1

    resumed = run(collect(service.resume_turn(db_real, admin_id, run_id, approved=True)))

    # 恢复后那次委派被上限挡住：回灌的结果里说明了原因，且没有第二个子 run
    tool_messages = [
        message for message in adapter.calls[-1]["messages"] if message["role"] == "tool"
    ]
    assert any("委派次数已达上限" in message["content"] for message in tool_messages)
    parent = db_real.query(AgentRun).filter(AgentRun.parent_run_id.is_(None)).one()
    children = db_real.query(AgentRun).filter(AgentRun.parent_run_id == parent.id).all()
    assert len(children) == 1
