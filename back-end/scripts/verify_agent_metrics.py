"""验证 /metrics/agents 的聚合 SQL。不依赖 pytest，直接 python 跑。

    python scripts/verify_agent_metrics.py

为什么要单独验：聚合查询的错法几乎全是**静默**的。join 写错会让行数翻倍（每个
span 一行，于是 runs 被算成 span 数）；分母选错会让委派率看起来很低；把逐 span 的
avg(duration_ms) 当成每次回答的延迟，得到的数字小一个数量级——而这三种错误都不会
报异常，只会给出一个看起来很合理的面板。

种的数据是刻意造出来的:2 次委派回答 + 2 次未委派回答 + 1 次子代理失败 + 1 次被
审批打断,这样每个指标的期望值都能手算出来。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from config import settings  # noqa: E402
from database import Base  # noqa: E402
import models  # noqa: E402,F401
from models import AgentRun, TraceSpan, User, Workspace  # noqa: E402
from routers.metrics_router import get_agent_metrics  # noqa: E402
from services.clock import naive_now  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(label)
        print(f"  [OK]   {label}")
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))


def seed(session, user_id: str) -> None:
    """5 次主代理执行，期望值全部可手算。

    - run A: 委派 1 次（子代理 researcher 成功），2 个 span，成本 0.030
    - run B: 委派 2 次（researcher 成功 + analyst 失败），成本 0.050
    - run C: 未委派，成本 0.008
    - run D: 未委派，成本 0.010
    - run E: 未委派，被审批打断 1 次，仍在 waiting_approval，成本 0.004
    """
    now = naive_now()
    plan = [
        ("A", 2, 1, 0, "done", Decimal("0.030"), 14_000),
        ("B", 5, 2, 0, "done", Decimal("0.050"), 23_000),
        ("C", 2, 0, 0, "done", Decimal("0.008"), 5_000),
        ("D", 3, 0, 0, "done", Decimal("0.010"), 7_000),
        ("E", 1, 0, 1, "waiting_approval", Decimal("0.004"), 3_000),
    ]
    for name, rounds, delegations, interrupts, status, cost, turn_ms in plan:
        trace_id = uuid.uuid4().hex
        run_id = f"run-{name}"
        session.add(
            AgentRun(
                id=run_id,
                chat_id="c1",
                user_id=user_id,
                message_id=f"m-{name}",
                agent_role=None,
                status=status,
                rounds=rounds,
                delegations=delegations,
                interrupts=interrupts,
                model="glm-4.5-air",
                trace_id=trace_id,
                started_at=now - timedelta(minutes=10),
                updated_at=now,
                finished_at=None if status == "waiting_approval" else now,
            )
        )
        # 根 span：整次回答的耗时。/metrics/agents 的延迟只看它
        session.add(
            TraceSpan(
                id=uuid.uuid4().hex[:32],
                trace_id=trace_id,
                parent_id=None,
                name="chat.turn",
                kind="agent",
                user_id=user_id,
                chat_id="c1",
                started_at=now - timedelta(minutes=10),
                duration_ms=turn_ms,
                status="ok",
                model="glm-4.5-air",
                prompt_tokens=1000,
                completion_tokens=200,
                token_source="provider",
                cost=cost / 2,
                currency="CNY",
            )
        )
        # 子 span：一次模型调用。故意给一个很小的 duration，
        # 好让"逐 span 平均"和"每次回答平均"明显不同
        session.add(
            TraceSpan(
                id=uuid.uuid4().hex[:32],
                trace_id=trace_id,
                parent_id="x" * 8,
                name="llm.completion",
                kind="llm",
                user_id=user_id,
                chat_id="c1",
                started_at=now - timedelta(minutes=9),
                duration_ms=900,
                status="ok",
                model="glm-4.5-air",
                prompt_tokens=800,
                completion_tokens=150,
                token_source="provider",
                cost=cost / 2,
                currency="CNY",
            )
        )

    # 子代理 run：A 一个成功，B 一个成功 + 一个失败
    children = [
        ("run-A", "researcher", "done", 2, None),
        ("run-B", "researcher", "done", 3, None),
        ("run-B", "analyst", "failed", 1, "subagent_failed"),
    ]
    for parent, role, status, rounds, error in children:
        session.add(
            AgentRun(
                id=uuid.uuid4().hex,
                chat_id="c1",
                user_id=user_id,
                message_id=None,
                parent_run_id=parent,
                agent_role=role,
                status=status,
                rounds=rounds,
                delegations=0,
                interrupts=0,
                error_type=error,
                started_at=now - timedelta(minutes=9),
                updated_at=now,
                finished_at=now,
            )
        )
    session.commit()


async def main() -> int:
    print("=" * 72)
    print("/metrics/agents 聚合验证（真 SQLite，数据刻意造成可手算）")
    print("=" * 72)

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    now = naive_now()
    session.add(Workspace(id="w1", name="验证", invite_code="V1", created_at=now))
    user = User(id="u1", email="v@example.com", username="v", role="admin", workspace_id="w1")
    session.add(user)
    session.commit()
    seed(session, "u1")

    settings.AGENT_CHECKPOINT_ENABLED = True
    settings.AGENT_DELEGATION_MODE = "augment"
    settings.AGENT_APPROVAL_MODE = "write"
    settings.METRICS_DEFAULT_DAYS = 7

    result = await get_agent_metrics(days=7, db=session, current_user=user)
    totals = result["totals"]

    print("\n[1] 总量：主代理 run 不被 join 放大")
    check("runs = 5（不是 span 数 10）", totals["runs"] == 5, f"runs={totals['runs']}")
    check("delegatedRuns = 2", totals["delegatedRuns"] == 2, f"got={totals['delegatedRuns']}")
    check("delegations = 3（A 一次 + B 两次）", totals["delegations"] == 3,
          f"got={totals['delegations']}")
    check("delegationRate = 0.4（2/5）", totals["delegationRate"] == 0.4,
          f"got={totals['delegationRate']}")
    check("interrupts = 1", totals["interrupts"] == 1, f"got={totals['interrupts']}")
    check("interruptedRuns = 1", totals["interruptedRuns"] == 1)
    check("waitingApproval = 1", totals["waitingApproval"] == 1)
    check("failedRuns = 0（子代理失败不算主代理失败）", totals["failedRuns"] == 0,
          f"got={totals['failedRuns']}")
    check("avgRounds = 2.6（(2+5+2+3+1)/5）", abs((totals["avgRounds"] or 0) - 2.6) < 1e-6,
          f"got={totals['avgRounds']}")

    print("\n[2] 角色分布：只统计子代理 run")
    by_role = {row["role"]: row for row in result["byRole"]}
    check("只有两个角色（不含主代理的 None）", set(by_role) == {"researcher", "analyst"},
          f"roles={sorted(by_role)}")
    check("researcher 2 次", by_role.get("researcher", {}).get("runs") == 2)
    check("analyst 1 次", by_role.get("analyst", {}).get("runs") == 1)
    check("analyst 失败率 1.0", by_role.get("analyst", {}).get("failureRate") == 1.0,
          f"got={by_role.get('analyst', {}).get('failureRate')}")
    check("researcher 失败率 0.0", by_role.get("researcher", {}).get("failureRate") == 0.0)

    print("\n[3] 委派 vs 未委派对比")
    comparison = {row["delegated"]: row for row in result["comparison"]}
    check("两个分桶都在", set(comparison) == {True, False}, f"buckets={sorted(comparison)}")
    delegated = comparison.get(True, {})
    plain = comparison.get(False, {})
    check("委派桶 runs = 2（去重后，不是 4 个 span）", delegated.get("runs") == 2,
          f"got={delegated.get('runs')}")
    check("未委派桶 runs = 3", plain.get("runs") == 3, f"got={plain.get('runs')}")
    check("委派成本 = 0.080（0.030+0.050）",
          abs(float(delegated.get("cost") or 0) - 0.080) < 1e-6,
          f"got={delegated.get('cost')}")
    check("未委派成本 = 0.022（0.008+0.010+0.004）",
          abs(float(plain.get("cost") or 0) - 0.022) < 1e-6,
          f"got={plain.get('cost')}")
    check("委派均价 = 0.04", abs(float(delegated.get("avgCost") or 0) - 0.04) < 1e-6,
          f"got={delegated.get('avgCost')}")
    check("委派比未委派贵",
          float(delegated.get("avgCost") or 0) > float(plain.get("avgCost") or 0))

    print("\n[4] 延迟：算的是每次回答，不是每个 span")
    # 委派：(14000+23000)/2 = 18500；未委派：(5000+7000+3000)/3 = 5000
    check("委派平均回合耗时 = 18500ms",
          abs((delegated.get("avgTurnMs") or 0) - 18500) < 1e-6,
          f"got={delegated.get('avgTurnMs')}")
    check("未委派平均回合耗时 = 5000ms",
          abs((plain.get("avgTurnMs") or 0) - 5000) < 1e-6,
          f"got={plain.get('avgTurnMs')}")
    check("延迟没有被子 span 拉低（远大于 900ms）",
          (delegated.get("avgTurnMs") or 0) > 900 and (plain.get("avgTurnMs") or 0) > 900)

    print("\n[5] 开关与空窗口的语义")
    check("enabled 反映快照开关", result["enabled"] is True)
    check("delegationMode 透出", result["delegationMode"] == "augment")

    settings.AGENT_CHECKPOINT_ENABLED = False
    off = await get_agent_metrics(days=7, db=session, current_user=user)
    check("快照关闭时 enabled = false", off["enabled"] is False)
    settings.AGENT_CHECKPOINT_ENABLED = True

    empty_user = User(id="u2", email="e@example.com", username="e", role="admin")
    session.add(empty_user)
    session.commit()
    empty = await get_agent_metrics(days=7, db=session, current_user=empty_user)
    check("没有数据时 runs = 0", empty["totals"]["runs"] == 0)
    check("没有数据时 delegationRate 是 None 而不是 0",
          empty["totals"]["delegationRate"] is None,
          f"got={empty['totals']['delegationRate']}")
    check("没有数据时 comparison 为空列表", empty["comparison"] == [])

    session.close()
    engine.dispose()

    print("\n" + "=" * 72)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for label in FAIL:
        print(f"  FAILED: {label}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
