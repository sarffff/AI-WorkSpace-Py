"""``submit_review`` 与审核台账。

这个文件测的是**接线**：结论从模型的一次工具调用，走到库里那一行。

为什么这条链值得单独测：它有三处"看起来成功的失败"。

1. **工具没注册。** 模型于是把结论写在回答文本里，而那正是这整套东西要取代的
   东西——不报错，只是结构化那一步从没发生过。
2. **版本号是模型给的。** 它会从上下文抄一个看起来合理的数字。一个抄错的版本号
   比没有版本号更糟：它看起来是可追溯的。
3. **落库失败被吞掉。** 工具回"记下了"，模型照着向用户复述"已归档"，而台账是空的。
"""
from __future__ import annotations

import json

import pytest

from config import settings
from conftest import run
from models import ReviewRecord
from services import review_service, review_tools, skill_library, skill_service
from services.review_consensus import ConsensusResult
from services.structured import InputCheck, ReviewVerdict


@pytest.fixture(autouse=True)
def _ledger_on(monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_LEDGER_ENABLED", True)
    monkeypatch.setattr(settings, "SKILL_ENABLED", True)


@pytest.fixture
def sop(tmp_path, monkeypatch):
    """一份声明了必备材料的内置 SOP。"""
    monkeypatch.setattr(skill_library, "SKILL_DIR", str(tmp_path))
    monkeypatch.setattr(skill_library, "_builtin", None)
    directory = tmp_path / "expense"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: expense\ndescription: 审核报销单\n"
        "required_inputs: 金额, 凭证\n---\n第一步：核对额度。",
        encoding="utf-8",
    )
    skill_library.reload()
    return tmp_path


def _tool(db, **over):
    kwargs = {"workspace_id": "w1", "user_id": "u1"}
    kwargs.update(over)
    defs = review_tools.build(db, **kwargs)
    return {tool.name: tool for tool in defs}


def _call(tool, **arguments):
    return run(tool.handler(arguments))


def _ok_args(**over):
    args = {
        "subject": "张三 3 月住宿",
        "sop_name": "expense",
        "inputs": [
            {"name": "金额", "value": "480", "found": True},
            {"name": "凭证", "value": "发票 #7712", "found": True},
        ],
        "verdict": "pass",
        "basis": ["额度标准：一线 600，申报 480，未超"],
        "evidence": "住宿费 480 元，发票号码 7712，一线城市上限 600。",
    }
    args.update(over)
    return args


# ========== 注册条件 ==========


def test_开关关掉时不注册(sop, db_real, monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_LEDGER_ENABLED", False)
    assert review_tools.build(db_real, workspace_id="w1", user_id="u1") == []


def test_没有审核型SOP时不注册(tmp_path, db_real, monkeypatch):
    """判据是 required_inputs 非空，不是"有没有 skill"。

    一个只放写作指导的部署给模型 submit_review 只会让它去猜该记什么。
    """
    monkeypatch.setattr(skill_library, "SKILL_DIR", str(tmp_path))
    monkeypatch.setattr(skill_library, "_builtin", None)
    directory = tmp_path / "writing"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: writing\ndescription: 写季度报告\n---\n按模板写。",
        encoding="utf-8",
    )
    skill_library.reload()
    assert review_tools.build(db_real, workspace_id="w1", user_id="u1") == []


def test_有审核型SOP时注册(sop, db_real):
    assert set(_tool(db_real)) == {"submit_review"}


def test_循环的工具面里真有submit_review(sop, db_real):
    """钉的是接线，不是工具本身。

    忘了在 ``_create_tools`` 里挂上 ``review_tools.build`` 时，上面所有用例都绿——
    它们直接调 ``review_tools.build``。模型永远看不到这个工具，结论继续写在回答里，
    而这正是整套东西要取代的形状。不报错。
    """
    from types import SimpleNamespace

    from services.chat_service import ChatService

    service = ChatService(model_adapter=None)
    names = {
        tool.name
        for tool in service._create_tools(
            db_real,
            SimpleNamespace(user_id="u1", workspace_id="w1", is_admin=True, history=[]),
            use_rag=False,
        )
    }
    assert "submit_review" in names



# ========== 落库 ==========


def test_结论真的写进台账(sop, db_real):
    """钉的是整条链：工具调用 → ReviewVerdict 校验 → 库里一行。

    只断言返回文本会漏掉最重要的那一步——"说记下了"和"真记下了"是两件事。
    """
    result = _call(
        _tool(db_real, chat_id="c1", message_id="m1")["submit_review"],
        **_ok_args(),
    )
    assert "已记入台账" in result

    rows = db_real.query(ReviewRecord).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.verdict == "pass"
    assert row.subject == "张三 3 月住宿"
    assert row.workspace_id == "w1" and row.user_id == "u1"
    # 线索留着：想看当时怎么审的，去这段对话
    assert row.chat_id == "c1" and row.message_id == "m1"
    # inputs / basis 是 JSON 字符串，逐项存下来给人复核
    assert [item["name"] for item in json.loads(row.inputs)] == ["金额", "凭证"]
    assert json.loads(row.basis) == ["额度标准：一线 600，申报 480，未超"]


def test_版本号由服务端填而不是模型(sop, db_real):
    """模型给的 sop_version 一律忽略。

    让它填的话它会从上下文抄一个看起来合理的数字，而这一列的全部意义在于事后能
    对上"当时按的哪一版"——抄错的版本号比没有版本号更糟，它看起来是可追溯的。

    内置 SOP 的版本是 0（跟 git 走，不由 workspace_skills 决定）。
    """
    _call(
        _tool(db_real)["submit_review"],
        **_ok_args(sop_version=999),
    )
    row = db_real.query(ReviewRecord).one()
    assert row.sop_version == 0, "内置 SOP 的版本号应当是 0，而不是模型给的 999"


def test_工作区SOP用它自己的版本号(sop, db_real):
    """工作区那份盖掉内置，版本号也该跟着它走。"""
    skill_service.upsert(
        db_real,
        "w1",
        name="expense",
        description="本公司报销流程",
        instructions="按两步走。",
        required_inputs="金额, 凭证",
    )
    # 再改一次，版本号涨到 2
    skill_service.upsert(
        db_real,
        "w1",
        name="expense",
        description="本公司报销流程",
        instructions="按三步走。",
        required_inputs="金额, 凭证",
    )
    _call(_tool(db_real)["submit_review"], **_ok_args())
    row = db_real.query(ReviewRecord).one()
    assert row.sop_version == 2


# ========== 必填槽位 ==========


def test_少一项必备材料时拒绝提交(sop, db_real):
    """"没检查"和"检查了没找到"处置相反：前者要它回去看，后者才是转人工。

    这一条在 ReviewVerdict 里做不了——那个类拿不到 skill，不知道该有几项。
    """
    result = _call(
        _tool(db_real)["submit_review"],
        **_ok_args(inputs=[{"name": "金额", "value": "480", "found": True}]),
    )
    assert "提交失败" in result
    assert "凭证" in result
    assert db_real.query(ReviewRecord).count() == 0


def test_缺料却给通过时被validator拦住(sop, db_real):
    """这是 A1 那道门在工具边界上的表现：校验失败，什么都没落库。"""
    result = _call(
        _tool(db_real)["submit_review"],
        **_ok_args(
            inputs=[
                {"name": "金额", "value": "480", "found": True},
                {"name": "凭证", "value": None, "found": False},
            ],
            verdict="pass",
        ),
    )
    assert "提交失败" in result
    assert db_real.query(ReviewRecord).count() == 0


def test_缺料给转人工时可以提交(sop, db_real):
    """反向：needs_human 必须走得通，否则正确的出口也被堵住了。"""
    result = _call(
        _tool(db_real)["submit_review"],
        **_ok_args(
            inputs=[
                {"name": "金额", "value": "480", "found": True},
                {"name": "凭证", "value": None, "found": False},
            ],
            verdict="needs_human",
        ),
    )
    assert "需要人判断" in result
    # 缺什么要说出来，模型据此向用户解释
    assert "凭证" in result
    row = db_real.query(ReviewRecord).one()
    assert row.verdict == "needs_human"


def test_没加载过的SOP名字被拒(sop, db_real):
    result = _call(_tool(db_real)["submit_review"], **_ok_args(sop_name="nope"))
    assert "提交失败" in result
    # 要列出可用的，否则模型只能猜
    assert "expense" in result
    assert db_real.query(ReviewRecord).count() == 0


def test_没交依据原文时拒绝提交(sop, db_real):
    """必填而不是可选：一条没有材料的结论**没法被独立检查**。

    允许它为空的话，模型会在拿不准时省掉这个参数——于是恰恰是最该复审的那些
    结论没有复审。
    """
    args = _ok_args()
    args.pop("evidence")
    result = _call(_tool(db_real)["submit_review"], **args)
    assert "提交失败" in result
    assert db_real.query(ReviewRecord).count() == 0


def test_依据原文超限时明确报错而不是截断(sop, db_real, monkeypatch):
    """截断掉的正好是尾部，而尾部常常是签字与日期。"""
    monkeypatch.setattr(settings, "REVIEW_EVIDENCE_MAX_CHARS", 600)
    result = _call(
        _tool(db_real)["submit_review"], **_ok_args(evidence="材" * 700)
    )
    assert "提交失败" in result
    assert "超过" in result
    assert db_real.query(ReviewRecord).count() == 0


def test_依据原文落库(sop, db_real):
    """人复核时要看的就是这一列。"""
    _call(_tool(db_real)["submit_review"], **_ok_args())
    row = db_real.query(ReviewRecord).one()
    assert row.evidence == "住宿费 480 元，发票号码 7712，一线城市上限 600。"


# ========== 独立复审 ==========


class _ReplayAdapter:
    """按脚本返回 JSON。复审要真的发一次模型调用，这里给它一个替身。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def complete(self, **kwargs):
        from services.model_adapter import ModelCompletion

        self.calls.append(kwargs)
        raw = self.script.pop(0) if self.script else None
        return ModelCompletion(content=raw or "", tool_calls=[])


def _resample(verdict="pass", name="金额", found=True, second="凭证"):
    """复审那一侧的返回。两项都要给，否则会被判成"形状对不上"。"""
    return (
        '{"inputs": ['
        '{"name": "%s", "value": "480", "found": %s}, '
        '{"name": "%s", "value": "有", "found": true}], '
        '"basis": ["复审依据"], "verdict": "%s", '
        '"sop_name": "expense", "sop_version": 0}'
        % (name, "true" if found else "false", second, verdict)
    )


def test_复审一致时照原样落库(sop, db_real, monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_CONSENSUS_RUNS", 2)
    adapter = _ReplayAdapter([_resample("pass")])
    tools = _tool(db_real, adapter=adapter)
    result = _call(tools["submit_review"], **_ok_args())

    assert "复审 2 次一致" in result
    row = db_real.query(ReviewRecord).one()
    assert row.verdict == "pass"
    assert row.runs == 2 and row.agreed is True


def test_复审不一致时落库的是转人工(sop, db_real, monkeypatch):
    """最该抓住的形状：模型自己提交 pass，独立复审给出 reject。

    落库必须是**合并之后**的结论。记模型那份的话，台账上是 pass 而回答里说
    转人工——或者反过来，而两者都比没有这个检查更糟。
    """
    monkeypatch.setattr(settings, "REVIEW_CONSENSUS_RUNS", 2)
    adapter = _ReplayAdapter([_resample("reject")])
    tools = _tool(db_real, adapter=adapter)
    result = _call(tools["submit_review"], **_ok_args(verdict="pass"))

    # 要让模型知道它自己那份没被采纳
    assert "需要人判断" in result
    assert "不要坚持你原来的结论" in result
    row = db_real.query(ReviewRecord).one()
    assert row.verdict == "needs_human"
    assert row.agreed is False
    assert row.runs == 2


def test_复审跑不通时照常落库但记未检查(sop, db_real, monkeypatch):
    """这次审核本身是成功的，复审只是加固。

    但 runs 要如实记 1——否则台账上看不出这条没被复审过。
    """
    monkeypatch.setattr(settings, "REVIEW_CONSENSUS_RUNS", 2)
    adapter = _ReplayAdapter([None])
    tools = _tool(db_real, adapter=adapter)
    result = _call(tools["submit_review"], **_ok_args())

    assert "已记入台账" in result
    assert "未做独立复审" in result
    row = db_real.query(ReviewRecord).one()
    assert row.verdict == "pass"
    assert row.runs == 1


def test_默认不开复审时不发额外调用(sop, db_real):
    """默认 REVIEW_CONSENSUS_RUNS=1：它让每次审核贵一倍，该由部署方决定。"""
    adapter = _ReplayAdapter([_resample("reject")])
    tools = _tool(db_real, adapter=adapter)
    result = _call(tools["submit_review"], **_ok_args())

    assert adapter.calls == [], "默认配置下不该发复审调用"
    assert "未做独立复审" in result
    row = db_real.query(ReviewRecord).one()
    assert row.verdict == "pass"


def test_台账记的runs是1表示没做一致性检查(sop, db_real):
    """runs=1 且 agreed=True 读作"没检查过"，不是"检查过并且一致"。

    默认 ``REVIEW_CONSENSUS_RUNS=1``：``verify_submission`` 提前返回、不重采样，
    于是落库 runs=1。复审本身是**接好的**（submit_review 调 verify_submission，见
    review_tools），只是默认不开——所以这条 runs=1 的含义是"这次没做一致性检查"，
    不是"检查过并且一致"。台账上这两者必须分得开，否则一条没检查过的结论会看起来
    像通过了复审。
    """
    _call(_tool(db_real)["submit_review"], **_ok_args())
    row = db_real.query(ReviewRecord).one()
    assert row.runs == 1 and row.agreed is True


# ========== 台账读取与复核 ==========


def _seed(db, verdict="needs_human", subject="单据 A"):
    return review_service.record(
        db,
        workspace_id="w1",
        user_id="u1",
        subject=subject,
        result=ConsensusResult(
            verdict=ReviewVerdict(
                inputs=[InputCheck(name="凭证", found=verdict != "needs_human")],
                basis=["依据一"],
                verdict=verdict,
                sop_name="expense",
                sop_version=1,
            ),
            runs=1,
            agreed=True,
        ),
    )


def test_待办筛选只给需要人看的(db_real):
    """转人工如果没有"待办在哪"的入口，它等于把结论扔进没人看的队列——
    那比直接给个错结论更难发现。"""
    _seed(db_real, "needs_human", "要人看的")
    _seed(db_real, "pass", "已通过的")

    every = review_service.list_for_workspace(db_real, "w1")
    pending = review_service.list_for_workspace(db_real, "w1", pending_only=True)
    assert len(every) == 2
    assert [item["subject"] for item in pending] == ["要人看的"]


def test_复核之后从待办里消失(db_real):
    row = _seed(db_real)
    review_service.resolve(
        db_real, "w1", row.id, user_id="admin", resolution="approved", note="我看过了"
    )
    assert review_service.list_for_workspace(db_real, "w1", pending_only=True) == []
    updated = review_service.list_for_workspace(db_real, "w1")[0]
    assert updated["resolution"] == "approved"
    assert updated["resolutionNote"] == "我看过了"
    assert updated["resolvedBy"] == "admin"


def test_已复核的不允许再改(db_real):
    """台账要能作为依据，而可以反复改写的记录作不了依据。"""
    row = _seed(db_real)
    review_service.resolve(
        db_real, "w1", row.id, user_id="admin", resolution="approved"
    )
    with pytest.raises(review_service.ReviewError) as caught:
        review_service.resolve(
            db_real, "w1", row.id, user_id="admin2", resolution="rejected"
        )
    assert "重新审一次" in str(caught.value)


def test_不能处置别的工作区的结论(db_real):
    """台账是组织资产，但不能让 A 工作区的人处置 B 的结论。"""
    row = _seed(db_real)
    with pytest.raises(review_service.ReviewError):
        review_service.resolve(
            db_real, "w2", row.id, user_id="attacker", resolution="approved"
        )


def test_坏掉的JSON不让整个台账打不开(db_real):
    """一条历史记录的 JSON 坏了，不该让台账页面整个失败。"""
    row = _seed(db_real)
    row.inputs = "{不是合法 JSON"
    db_real.commit()
    items = review_service.list_for_workspace(db_real, "w1")
    assert items[0]["inputs"] == []
    # 其余字段照常可读
    assert items[0]["verdict"] == "needs_human"
