"""编排层的两块接线：子代理委派与 SOP 作业指导。

文档§3 把这两样放在能力层与编排层里，但"模块存在"和"模型能用"是两件事。
这里钉的是后者，全是"没接上也不会报错"的那类缺口：

- 工具面：委派开着时 ``delegate`` 真的注册了、supervisor 真的把查询工具收走了、
  没接政策检索时 ``search_policy`` 真的不出现（注册了就是邀请模型烧一轮）。
- 记账：子代理内部的工具调用与模型成本必须并回这张工单，否则
  ``TICKET_MAX_TOOL_CALLS`` 和单工单成本上限在开委派之后都只是主代理那一圈的上限。
- 边界：子代理执行不了写操作——审批闸门在主循环按 proposals 判，
  而它跑在工具处理器里，够不着那道门。
- SOP：索引和 ``load_skill`` 必须同进同出；加载过的名单要写回状态，
  否则挂起恢复之后同一份正文会被再注入一遍。

模型一律用 ``ScriptedAdapter`` 回放；子代理共用同一个适配器，所以脚本里
"主代理那一轮"之后紧接着就是"子代理的每一轮"。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from config import settings
from conftest import ScriptedAdapter, run
from models import CsCustomer, CsOperation, CsOrder, CsRefund, TicketEvent
from services import agent_roles, pricing
from services.clock import naive_now
from services.ticket import tools as cs
from services.ticket.graph import TicketRuntime, run_ticket
from services.ticket.intake import TicketIntake, submit_ticket

WS = "w1"
USER = "seat1"
ORDER = "ORD20260115001"


@pytest.fixture(autouse=True)
def _ticket_settings(monkeypatch):
    monkeypatch.setattr(settings, "TICKET_CHANNELS", "web_chat,email,app,api")
    monkeypatch.setattr(settings, "TICKET_UNDERSTAND_LLM", False)
    monkeypatch.setattr(settings, "TICKET_REFUND_REVIEW_THRESHOLD", 200.0)
    monkeypatch.setattr(settings, "TICKET_NEGATIVE_KEYWORDS", "垃圾,太差,骗子")
    monkeypatch.setattr(settings, "TICKET_LEGAL_KEYWORDS", "律师,起诉,315")
    monkeypatch.setattr(settings, "STRUCTURED_OUTPUT_RETRIES", 0)
    monkeypatch.setattr(settings, "TICKET_MAX_TOOL_CALLS", 12)
    monkeypatch.setattr(settings, "TICKET_DAILY_REFUND_LIMIT", 1000.0)
    monkeypatch.setattr(settings, "TICKET_PLAN_ENABLED", False)
    monkeypatch.setattr(settings, "AGENT_MAX_DELEGATIONS", 2)
    monkeypatch.setattr(settings, "AGENT_SUBAGENT_RESULT_MAX_CHARS", 4000)
    monkeypatch.setattr(settings, "SKILL_MAX_LOADS", 3)


def _seed_order(db):
    now = naive_now()
    db.add(
        CsCustomer(
            id="cust1", workspace_id=WS, display_name="张三", email="zhang@corp.com",
            created_at=now, updated_at=now,
        )
    )
    db.add(
        CsOrder(
            id="ord1", workspace_id=WS, customer_id="cust1", order_no=ORDER,
            status="paid", total_amount="350.00", refunded_amount="0.00",
            receiver_name="张三", receiver_phone="13800138000",
            address_text="北京市海淀区中关村大街1号", created_at=now, updated_at=now,
        )
    )
    db.commit()


def _ticket(db, text: str = f"订单 {ORDER} 帮我查一下状态") -> Ticket:
    return submit_ticket(
        db,
        TicketIntake(channel="web_chat", content=text, customer_email="zhang@corp.com"),
        workspace_id=WS,
        assignee_id=USER,
    ).ticket


def _runtime(db, ticket, rounds, tmp_path, *, policy=None) -> TicketRuntime:
    return TicketRuntime(
        db=db,
        workspace_id=WS,
        user_id=USER,
        adapter=ScriptedAdapter(rounds),
        ticket=ticket,
        model="test-model",
        policy_search=policy,
        checkpoint_path=str(tmp_path / "checkpoints.db"),
    )


def _tools_of(runtime: TicketRuntime, index: int = 0) -> list[str]:
    """第 ``index`` 次模型调用实际收到的工具名。"""
    return runtime.adapter.calls[index]["tools"]


def _events(db, ticket_id: str):
    return (
        db.query(TicketEvent)
        .filter_by(ticket_id=ticket_id)
        .order_by(TicketEvent.seq.asc())
        .all()
    )


def _messages(result: dict):
    return result["state"]["messages"]


# ========== 工具面 ==========


def test_默认关着时没有委派工具而政策检索照旧(db_real, tmp_path):
    """委派是模式开关，政策通道不是——接了检索就有工具，和委不开不开无关。

    注册了用不了的工具是最贵的一种"功能存在"：模型每轮都看到它、试一次、
    拿回一句失败，而账单上看不出来。反过来，``search_policy`` 在没接检索时才消失
    （下面那条）。
    """
    _seed_order(db_real)
    ticket = _ticket(db_real)

    async def policy(query: str) -> str:
        return "退货政策：7 天无理由。"

    runtime = _runtime(
        db_real, ticket, [{"text": "您这单是已付款状态。"}], tmp_path, policy=policy
    )
    result = run(run_ticket(runtime))

    assert result["outcome"] == "resolved"
    seen = _tools_of(runtime)
    assert "delegate" not in seen
    assert "search_policy" in seen
    assert "lookup_order" in seen


def test_没接政策检索时不注册检索通道(db_real, tmp_path):
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    runtime = _runtime(db_real, ticket, [{"text": "已付款。"}], tmp_path)
    run(run_ticket(runtime))

    seen = _tools_of(runtime)
    assert "delegate" in seen, "augment 模式下委派工具必须真的下发"
    assert "search_policy" not in seen
    monkeypatch.undo()


def test_augment_保留全部工具并多出委派(db_real, tmp_path):
    _seed_order(db_real)
    ticket = _ticket(db_real)

    async def policy(query: str) -> str:
        return "退货政策：7 天无理由。"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    runtime = _runtime(
        db_real, ticket, [{"text": "已付款。"}], tmp_path, policy=policy
    )
    run(run_ticket(runtime))

    seen = _tools_of(runtime)
    assert "lookup_order" in seen and "delegate" in seen and "search_policy" in seen
    monkeypatch.undo()


def test_supervisor_把查询类工具从主代理手里收走(db_real, tmp_path):
    """文档§3 的「子 Agent（查询）」的另一种落法：主代理只做决策与写操作。

    判据是权限档位而不是再点一份名单——而 ``test_write_tier_tools_are_in_no_role``
    保证角色拿到的全是 READ，所以被收走的工具一定有人能调。
    """
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "supervisor")
    runtime = _runtime(db_real, ticket, [{"text": "已付款。"}], tmp_path)
    run(run_ticket(runtime))

    seen = _tools_of(runtime)
    read_tools = {name for name, tier in cs.TIER_BY_TOOL.items() if tier == cs.READ}
    mutate_tools = {
        name for name, tier in cs.TIER_BY_TOOL.items() if tier != cs.READ
    }
    assert not (read_tools & set(seen)), "supervisor 下查询类工具应全部在角色手里"
    assert mutate_tools & set(seen), "写操作仍归主代理——委派出去的执行不过人在回路"
    assert "delegate" in seen
    monkeypatch.undo()


def test_有_sop_时索引与加载工具同进同出(db_real, tmp_path):
    """索引最后一句是"需要时调用 load_skill 取"。开关关着时那个工具不存在，
    发索引等于指示模型做一件做不了的事，而失败是静默的。"""
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "SKILL_ENABLED", True)
    runtime = _runtime(db_real, ticket, [{"text": "已付款。"}], tmp_path)
    result = run(run_ticket(runtime))

    assert "load_skill" in _tools_of(runtime)
    index = [
        message
        for message in _messages(result)
        if message["role"] == "system" and "作业指导" in message["content"]
    ]
    assert index, "SOP 索引必须作为 system 消息注入"
    assert "refund-playbook" in index[0]["content"]
    # 内置 refund-playbook 没有附带文件，所以不该多出 read_skill_file 这个工具名
    assert "read_skill_file" not in _tools_of(runtime)
    monkeypatch.undo()


def test_委派开着时系统消息里有角色清单(db_real, tmp_path):
    """策略说明必须列出**这一轮真的注册了**的角色：写一个不存在的角色名，
    模型会去派它，然后白烧一轮。"""
    _seed_order(db_real)
    ticket = _ticket(db_real)

    async def policy(query: str) -> str:
        return "退货政策：7 天无理由。"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    runtime = _runtime(
        db_real, ticket, [{"text": "已付款。"}], tmp_path, policy=policy
    )
    result = run(run_ticket(runtime))

    notice = [
        message
        for message in _messages(result)
        if message["role"] == "system" and "子代理" in message["content"]
    ]
    assert notice
    for name in ("inquiry", "policy", "reassurance"):
        assert name in notice[0]["content"]
    assert "写操作" in notice[0]["content"]
    monkeypatch.undo()


def test_没接政策检索时通知里也不列政策角色(db_real, tmp_path):
    """角色清单与 ``delegate`` 的 enum 来自同一个列表，所以两处必须一起少一个——
    否则模型派给 policy 时会拿回"没有这个子代理"，而那句失败话里列的可用清单
    本来就说不存在。"""
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    runtime = _runtime(db_real, ticket, [{"text": "已付款。"}], tmp_path)
    result = run(run_ticket(runtime))

    notice = [
        message["content"]
        for message in _messages(result)
        if message["role"] == "system" and "子代理" in message["content"]
    ][0]
    assert "inquiry" in notice and "policy" not in notice
    assert "search_policy" not in _tools_of(runtime, 0)
    monkeypatch.undo()


# ========== 委派跑通 ==========


def _delegate_round(role: str, task: str) -> dict:
    return {"tool_calls": [("delegate", {"role": role, "task": task})]}


def test_委派执行子代理并把报告回灌给主代理(db_real, tmp_path):
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    runtime = _runtime(
        db_real,
        ticket,
        [
            _delegate_round("inquiry", f"查订单 {ORDER} 的状态与金额"),
            {"tool_calls": [("lookup_order", {"order_no": ORDER})]},
            {"text": "订单已付款，实付 350 元，尚未发货。"},
            {"text": "您这单还没发货，预计明天出。"},
        ],
        tmp_path,
    )
    result = run(run_ticket(runtime))
    monkeypatch.undo()

    assert result["outcome"] == "resolved"
    report = [
        message["content"]
        for message in _messages(result)
        if message["role"] == "tool"
    ][0]
    assert "来自 inquiry 子代理的报告" in report
    assert "已付款" in report, "子代理真的查过业务系统，报告里带回了查到的内容"
    # 子代理那一轮收到的是按角色裁过的面：没有 delegate（递归委派没有上界），
    # 也没有它用不上的政策工具
    assert _tools_of(runtime, 1) == list(agent_roles.ROLES["inquiry"].tools)
    assert "delegate" not in _tools_of(runtime, 1)


def test_子代理内部的调用计入工单的调用数(db_real, tmp_path):
    """``TICKET_MAX_TOOL_CALLS`` 是治理上限。委派对主循环只是"一次工具调用"，
    而它内部是一个 3~5 轮的循环——不并进来的话，这条上限开委派之后就是假的。"""
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    runtime = _runtime(
        db_real,
        ticket,
        [
            _delegate_round("inquiry", f"查订单 {ORDER}"),
            {"tool_calls": [("lookup_order", {"order_no": ORDER})]},
            {"tool_calls": [("lookup_logistics", {"order_no": ORDER})]},
            {"text": "订单已付款；这单还没有发货记录。"},
            {"text": "还没发货。"},
        ],
        tmp_path,
    )
    result = run(run_ticket(runtime))
    monkeypatch.undo()

    # 一次 delegate + 子代理内部的两次查询
    assert result["state"]["calls_used"] == 3


def test_委派的成本并回单工单成本(db_real, tmp_path):
    """提供商的用量是覆盖写，所以子代理每一轮单独计一次；这里连主代理那两轮
    一起核对总数，确认两边都没漏。"""
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    monkeypatch.setattr(pricing, "cost_of_span", lambda span, model: (Decimal("0.5"), True))
    runtime = _runtime(
        db_real,
        ticket,
        [
            _delegate_round("inquiry", f"查订单 {ORDER}"),
            {"tool_calls": [("lookup_order", {"order_no": ORDER})]},
            {"text": "已付款。"},
            {"text": "还没发货。"},
        ],
        tmp_path,
    )
    result = run(run_ticket(runtime))
    monkeypatch.undo()

    assert runtime.delegated_cost == pytest.approx(1.0), "子代理两轮各 0.5"
    assert result["state"]["cost_used"] == pytest.approx(2.0), "主代理两轮 + 子代理两轮"
    assert result["state"]["cost_known"] is True
    assert float(ticket.llm_cost) == pytest.approx(2.0), "落库的是含委派的总成本"


def test_超过上限的委派被拒绝(db_real, tmp_path):
    """每次委派是一整个子代理循环，成本与延迟都上去，而它在单次调用里看不出来。"""
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    monkeypatch.setattr(settings, "AGENT_MAX_DELEGATIONS", 1)
    runtime = _runtime(
        db_real,
        ticket,
        [
            _delegate_round("inquiry", f"查订单 {ORDER}"),
            {"text": "已付款。"},
            _delegate_round("inquiry", "再查一次这单的物流"),
            {"text": "还没发货，物流部分没查到。"},
        ],
        tmp_path,
    )
    result = run(run_ticket(runtime))
    monkeypatch.undo()

    refusals = [
        message["content"]
        for message in _messages(result)
        if message["role"] == "tool" and "委派失败" in message["content"]
    ]
    assert refusals and "上限 1 次" in refusals[0]
    # 被拒的那次委派根本没有启动子代理：4 次调用 = 主 1 + 子 1 + 主 2
    assert len(runtime.adapter.calls) == 4


def test_派一个此刻不存在的角色时回灌可用清单(db_real, tmp_path):
    """``delegate`` 的 enum 由这一轮注册的角色生成，但提供商不保证校验 enum。

    编出来的名字在这里被挡下时，回灌的那句必须列出可用的是哪些——模型下一步
    该换名字还是放弃，取决于它看得见有什么。
    """
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    runtime = _runtime(
        db_real,
        ticket,
        [
            # 没有接政策检索，所以 policy 角色不在本轮清单里
            _delegate_round("policy", "查退货政策"),
            {"text": "这一条我先如实说明。"},
        ],
        tmp_path,
    )
    result = run(run_ticket(runtime))
    monkeypatch.undo()

    refusal = [
        message["content"]
        for message in _messages(result)
        if message["role"] == "tool" and "委派失败" in message["content"]
    ][0]
    assert "没有 'policy'" in refusal and "inquiry" in refusal
    # 一次模型调用都没多花：子代理根本没起来
    assert len(runtime.adapter.calls) == 2


def test_子代理执行不了写操作(db_real, tmp_path):
    """资金类工具注册在主代理的面上（高风险工单），但不在任何角色的工具面里。

    这是"委派不过人在回路"那条推理的落点：如果哪天有人往 inquiry 的
    ``tools`` 里加一个 ``create_refund``，这笔退款就会绕开 ``await_approval``
    直接执行，而账本上的 ``approved_by`` 是坐席自己的 id——看起来像批过。
    """
    _seed_order(db_real)
    ticket = _ticket(
        db_real, f"订单 {ORDER} 商品有质量问题，要求退款 350 元，请尽快处理"
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "AGENT_DELEGATION_MODE", "augment")
    runtime = _runtime(
        db_real,
        ticket,
        [
            _delegate_round("inquiry", f"查订单 {ORDER} 并直接退款 350 元"),
            {"tool_calls": [("create_refund", {"order_no": ORDER, "amount": "350"})]},
            {"text": "报告：我不能执行退款，只查到订单状态。"},
            {"text": "退款需要人工确认，已为您提交。"},
        ],
        tmp_path,
    )
    result = run(run_ticket(runtime))
    monkeypatch.undo()

    assert ticket.risk_level == "high", "这条测试的前提：资金类工具确实注册了"
    assert "create_refund" in _tools_of(runtime)
    steps = [
        event
        for event in _events(db_real, ticket.id)
        if event.tool_name == "create_refund"
    ]
    assert steps and "子代理 inquiry" in steps[0].message
    assert db_real.query(CsOperation).count() == 0, "委派出去的执行一条账都不该留下"
    assert db_real.query(CsRefund).count() == 0
    assert result["outcome"] == "resolved"


# ========== 政策检索通道 ==========


def test_模型可以再查一次政策而且原文被定界包住(db_real, tmp_path):
    """retrieve 节点那一次预检索是在**还没读完工单**时按"意图 + 订单号 + 原文前
    200 字"发起的；执行到一半问题收窄成"运费谁出"时，它多半没命中。

    这条通道进上下文的处理必须和 retrieve 那条完全一致——两条通道一条洗过一条
    没洗，等于让"这句有没有被定界保护"取决于它在第几步被查。
    """
    _seed_order(db_real)
    ticket = _ticket(db_real)
    queries: list[str] = []

    async def policy(query: str) -> str:
        queries.append(query)
        return "退货运费：质量问题由卖家承担，其他情形由买家承担。"

    runtime = _runtime(
        db_real,
        ticket,
        [
            {"tool_calls": [("search_policy", {"query": "退货运费由谁承担"})]},
            {"text": "如果是质量问题，运费我们来承担。"},
        ],
        tmp_path,
        policy=policy,
    )
    result = run(run_ticket(runtime))

    # 第一次是 retrieve 节点的预检索（按意图 + 订单号 + 原文拼的），第二次才是
    # 模型自己收窄后的检索词——这两次用的必须是同一个后端
    assert queries[0] != "退货运费由谁承担"
    assert queries[-1] == "退货运费由谁承担"
    fetched = [
        message["content"]
        for message in _messages(result)
        if message["role"] == "tool" and "运费" in message["content"]
    ]
    assert fetched and "政策资料" in fetched[0]
    assert "忽略" not in fetched[0]


def test_查不到政策时说明这不等于没有规定(db_real, tmp_path):
    """回一段空文本会让模型把"没查到"读成"没有规定"，然后按后者办事。"""
    _seed_order(db_real)
    ticket = _ticket(db_real)

    async def policy(query: str) -> str:
        return ""

    runtime = _runtime(
        db_real,
        ticket,
        [
            {"tool_calls": [("search_policy", {"query": "超过 30 天还能退吗"})]},
            {"text": "这一条我需要再确认，先为您转人工。"},
        ],
        tmp_path,
        policy=policy,
    )
    result = run(run_ticket(runtime))

    empty_handed = [
        message["content"]
        for message in _messages(result)
        if message["role"] == "tool" and "没有检索到" in message["content"]
    ]
    assert empty_handed and "不等于没有规定" in empty_handed[0]


# ========== SOP 加载 ==========


def test_load_skill_只注入一次正文(db_real, tmp_path):
    """同一份 SOP 灌两遍几千字规程，一个新信息都不带来，而它是这份上下文里
    最贵的那一段。第二次的返回值就是一句提醒。"""
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "SKILL_ENABLED", True)
    runtime = _runtime(
        db_real,
        ticket,
        [
            {"tool_calls": [("load_skill", {"name": "refund-playbook"})]},
            {"tool_calls": [("load_skill", {"name": "refund-playbook"})]},
            {"tool_calls": [("load_skill", {"name": "不存在的SOP"})]},
            {"text": "按规程先核对了订单状态。"},
        ],
        tmp_path,
    )
    result = run(run_ticket(runtime))
    monkeypatch.undo()

    tool_messages = [
        message["content"] for message in _messages(result) if message["role"] == "tool"
    ]
    bodies = [text for text in tool_messages if "[作业指导《refund-playbook》]" in text]
    assert len(bodies) == 1, "正文只注入一次"
    assert "已经在这张工单上加载过了" in tool_messages[1]
    # 名字写错时把可用的列出来：模型下一步该换名字还是放弃，都需要知道有哪些
    assert "可用的" in tool_messages[2] and "refund-playbook" in tool_messages[2]
    assert result["state"]["loaded_skills"] == ["refund-playbook"]


def test_超过份数上限的加载被拒绝(db_real, tmp_path):
    """没有上限时模型会把索引里每一份都加载一遍再开始干活，那是最贵的一种"稳妥"。"""
    _seed_order(db_real)
    ticket = _ticket(db_real)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "SKILL_ENABLED", True)
    monkeypatch.setattr(settings, "SKILL_MAX_LOADS", 1)
    runtime = _runtime(
        db_real,
        ticket,
        [
            {"tool_calls": [("load_skill", {"name": "refund-playbook"})]},
            {"tool_calls": [("load_skill", {"name": "refund-playbook"})]},
            {"text": "已按规程处理。"},
        ],
        tmp_path,
    )
    result = run(run_ticket(runtime))
    monkeypatch.undo()

    assert result["state"]["loaded_skills"] == ["refund-playbook"]
