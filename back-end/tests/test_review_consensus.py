"""结论不一致就转人工。

这个文件测的是**合并判据**，不是"跑几次"（那是调用方的事）。

背景：2026-09-12 实测同一条用例三轮，工具序列逐次相同而分数 5/5/3 与 5/3/5，
温度 0.0。所以摆动在结论层、改配置修不了，只能在产品层兜住。判据必须比字段
而不是比文本——措辞变了但结论相同（假阳）、措辞相同但金额差一位（假阴），
两头都会错。
"""
from __future__ import annotations

import pytest

from conftest import run
from services.review_consensus import combine
from services.structured import InputCheck, ReviewVerdict


def _v(verdict="pass", *, found=None, basis=None, version=1):
    """造一份结论。found 是 {项名: 找到了没有}。"""
    items = [
        InputCheck(name=name, found=ok, value="有" if ok else None)
        for name, ok in (found or {"金额": True}).items()
    ]
    return ReviewVerdict(
        inputs=items,
        basis=basis or [],
        verdict=verdict,
        sop_name="expense-review",
        sop_version=version,
    )


# ========== 一致 ==========


def test_两次结论相同就照原样交出去():
    """一致时不该合成新东西——原样返回第一份，包括它的 basis。"""
    result = combine([_v("pass", basis=["按第 3 条"]), _v("pass")])
    assert result.agreed is True
    assert result.verdict.verdict == "pass"
    assert result.verdict.basis == ["按第 3 条"]
    assert result.runs == 2
    assert result.reasons == ()


def test_只跑一次时不做检查():
    """N=1 没有可比的对象。返回原样并把 runs 记成 1，让上层知道这次没检查过。"""
    result = combine([_v("pass")])
    assert result.agreed is True
    assert result.runs == 1


def test_没有结论可合并时抛错():
    """这里没有安全的默认值：给 needs_human 会掩盖调用方的 bug，给 pass 更糟。"""
    with pytest.raises(ValueError):
        combine([])


# ========== 不一致 ==========


def test_结论不同就转人工():
    result = combine([_v("pass"), _v("reject")])
    assert result.agreed is False
    assert result.verdict.verdict == "needs_human"
    assert "不同结论" in result.reasons[0]
    # 计数要写出来，人才知道分歧有多大（2:1 和 1:1 的可信度不一样）
    assert "pass×1" in result.reasons[0] and "reject×1" in result.reasons[0]


def test_不做多数表决():
    """三次里两次 pass 也不通过。

    实测 5/5/3 与 5/3/5 两种形状都出现过，同一条用例在不同批次里多数票会翻面。
    多数表决会把"这题它拿不准"变成"这题它答对了 2/3"。
    """
    result = combine([_v("pass"), _v("pass"), _v("reject")])
    assert result.verdict.verdict == "needs_human"
    assert result.agreed is False


def test_材料项的分歧也算不一致():
    """found 不同说明两次**看到的材料不一样**，那时哪个 verdict 对都不重要了。"""
    result = combine(
        [
            _v("needs_human", found={"金额": True, "凭证": False}),
            _v("pass", found={"金额": True, "凭证": True}),
        ]
    )
    assert result.agreed is False
    # 要点名是哪一项，人才知道该核对什么
    assert any("凭证" in reason for reason in result.reasons)


def test_verdict相同但材料分歧仍然拦住():
    """这条最容易漏：只比 verdict 的话它会被判成一致。

    两次都说"需要人判断"，但一次是因为凭证没找到、一次是因为金额没找到——
    交给人的清单会漏掉其中一项。
    """
    result = combine(
        [
            _v("needs_human", found={"金额": True, "凭证": False}),
            _v("needs_human", found={"金额": False, "凭证": True}),
        ]
    )
    assert result.agreed is False
    assert len(result.reasons) == 2


def test_项数不同时说清形状对不上():
    result = combine(
        [
            _v("pass", found={"金额": True}),
            _v("pass", found={"金额": True, "凭证": True}),
        ]
    )
    assert result.agreed is False
    assert any("形状" in reason for reason in result.reasons)


# ========== 合成出来的那一份 ==========


def test_材料取最保守的那一份():
    """任一轮说没找到就算没找到。

    这一列是给人复核的清单，"有一轮没找到"正是要人去看的地方；取乐观值会让
    那一项从清单上消失——而消失了就没人会去核对它。
    """
    # 注意这两份的 verdict：缺料那一份只能是 needs_human，否则它自己就构造不出来
    # （ReviewVerdict 的 validator）。这正是真实会出现的分歧形状——
    # 一轮找到了凭证于是通过，另一轮没找到于是转人工。
    result = combine(
        [
            _v("pass", found={"金额": True, "凭证": True}),
            _v("needs_human", found={"金额": True, "凭证": False}),
        ]
    )
    found = {item.name: item.found for item in result.verdict.inputs}
    assert found == {"金额": True, "凭证": False}


def test_分歧说明排在依据最前面():
    """人要先看的是"为什么交到我手上"，然后才是"它当时怎么想的"。"""
    result = combine(
        [
            _v("pass", basis=["按第 3 条：上限 500"]),
            _v("reject", basis=["按第 5 条：超期"]),
        ]
    )
    basis = result.verdict.basis
    assert basis[0].startswith("⚠")
    # 每一轮的依据都留着，并标明是第几次——人判断时这比最终结论有用
    assert any("[第 1 次] 按第 3 条：上限 500" == line for line in basis)
    assert any("[第 2 次] 按第 5 条：超期" == line for line in basis)


def test_合成的结论保留SOP版本():
    """审核结论引用 sop_version。合成时丢掉它，台账上这条就失去依据。"""
    result = combine([_v("pass", version=7), _v("reject", version=7)])
    assert result.verdict.sop_version == 7
    assert result.verdict.sop_name == "expense-review"


def test_合成出来的结论本身合法_():
    """它要能通过 ReviewVerdict 自己的 validator——有 found=false 时必须是
    needs_human，而合成的正是 needs_human。构造成功即证明。"""
    result = combine(
        [
            _v("pass", found={"凭证": True}),
            _v("needs_human", found={"凭证": False}),
        ]
    )
    assert result.verdict.verdict == "needs_human"
    assert [i.found for i in result.verdict.inputs] == [False]


# ========== generate：采样 N 次 ==========


class _Adapter:
    """按脚本依次返回 JSON 串。None 表示这一次"跑不通"。"""

    def __init__(self, script: list[str | None]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        raw = self.script.pop(0) if self.script else None
        from services.model_adapter import ModelCompletion

        return ModelCompletion(content=raw or "", tool_calls=[])


def _json(verdict="pass", found=True, name="金额"):
    """复审那一侧返回的 JSON。

    ``name`` 默认和 ``_v()`` 的默认项名一致（金额）：项名对不上会被 combine
    正确判成"形状对不上"，那时测的就不是 verdict 的分歧了。
    """
    return (
        '{"inputs": [{"name": "%s", "value": "有", "found": %s}], '
        '"basis": ["按第 3 条"], "verdict": "%s", '
        '"sop_name": "expense-review", "sop_version": 2}'
        % (name, "true" if found else "false", verdict)
    )


def test_采样两次一致时算通过(monkeypatch):
    from services import review_consensus

    adapter = _Adapter([_json("pass"), _json("pass")])
    result, reports = run(
        review_consensus.generate(
            adapter, prompt="审这张单子", model="m", runs=2
        )
    )
    assert result is not None
    assert result.runs == 2 and result.agreed is True
    assert result.verdict.verdict == "pass"
    assert len(reports) == 2


def test_采样两次不一致就转人工():
    from services import review_consensus

    adapter = _Adapter([_json("pass"), _json("reject")])
    result, _reports = run(
        review_consensus.generate(
            adapter, prompt="审这张单子", model="m", runs=2
        )
    )
    assert result.agreed is False
    assert result.verdict.verdict == "needs_human"


def test_部分跑不通时用剩下的继续():
    """"少一个样本"和"结论不一致"是两件事，前者不该让整次审核失败。

    但只剩一份时 runs 记成 1——上层据此知道这次**实际没检查过**，
    而不是"检查过并且一致"。这两者在最终 verdict 上完全同形。
    """
    from services import review_consensus

    adapter = _Adapter([_json("pass"), None])
    result, reports = run(
        review_consensus.generate(
            adapter, prompt="审这张单子", model="m", runs=2
        )
    )
    assert result is not None
    assert result.runs == 1, "只拿到一份就该说只有一份，不能谎称检查过两次"
    assert result.agreed is True
    assert len(reports) == 2, "两次都要留报告，否则看不出有一次没跑通"


def test_全部跑不通时返回None而不是通过():
    """这是这个模块最重要的一条：审核失败的正确处置是承认失败。

    退回一个 pass 会让"模型没答上来"变成"这张单子合规"，而那是这个产品里
    最危险的一种失败——它不报错，而且方向是放行。
    """
    from services import review_consensus

    adapter = _Adapter([None, None])
    result, reports = run(
        review_consensus.generate(
            adapter, prompt="审这张单子", model="m", runs=2
        )
    )
    assert result is None
    assert len(reports) == 2


def test_温度不是零():
    """自洽性检查要的是独立采样，而 0.0 是在请求同一条贪心路径。

    钉住它是因为"把温度设成 0 更严谨"是个很自然的直觉，而在这里它会让整个
    机制失效：两次采到同一条路径，分歧永远测不出来。
    """
    from services import review_consensus

    adapter = _Adapter([_json("pass"), _json("pass")])
    run(
        review_consensus.generate(
            adapter, prompt="审这张单子", model="m", runs=2
        )
    )
    assert adapter.calls, "适配器没被调用"
    for call in adapter.calls:
        assert call.get("temperature", 0.0) > 0.0


# ========== verify_submission：提交的那份算第一个样本 ==========


def test_runs为1时不发复审调用():
    """没有可比的对象。返回的 runs=1 按 combine 的约定读作"没检查过"。"""
    from services import review_consensus

    submitted = _v("pass")
    adapter = _Adapter([_json("reject")])
    result = run(
        review_consensus.verify_submission(
            adapter,
            submitted=submitted,
            instructions="按额度审。",
            materials="住宿 480",
            required_inputs=("金额",),
            runs=1,
        )
    )
    assert result.runs == 1 and result.agreed is True
    assert result.verdict.verdict == "pass"
    assert adapter.calls == [], "runs=1 时不该发任何复审调用"


def test_提交的那份和复审一致时通过():
    from services import review_consensus

    submitted = _v("pass")
    adapter = _Adapter([_json("pass")])
    result = run(
        review_consensus.verify_submission(
            adapter,
            submitted=submitted,
            instructions="按额度审。",
            materials="住宿 480",
            required_inputs=("金额",),
            runs=2,
        )
    )
    assert result.agreed is True
    assert result.verdict.verdict == "pass"
    assert len(adapter.calls) == 1, "runs=2 时只该复审 1 次（提交的那份算第一个样本）"


def test_提交的那份和复审不一致就转人工():
    """这是这条链最该抓住的形状：模型自己说通过，独立采样说不通过。"""
    from services import review_consensus

    submitted = _v("pass")
    adapter = _Adapter([_json("reject")])
    result = run(
        review_consensus.verify_submission(
            adapter,
            submitted=submitted,
            instructions="按额度审。",
            materials="住宿 480",
            required_inputs=("金额",),
            runs=2,
        )
    )
    assert result.agreed is False
    assert result.verdict.verdict == "needs_human"
    assert any("不同结论" in r for r in result.reasons)


def test_复审失败不让提交失败():
    """这次审核本身是成功的，复审只是加固。让加固的故障吃掉一条有效结论，
    是把可观测性变成单点故障。但 runs 必须如实记成 1，否则台账上看不出
    这条没被复审过。"""
    from services import review_consensus

    submitted = _v("pass")
    adapter = _Adapter([None])
    result = run(
        review_consensus.verify_submission(
            adapter,
            submitted=submitted,
            instructions="按额度审。",
            materials="住宿 480",
            required_inputs=("金额",),
            runs=2,
        )
    )
    assert result.verdict.verdict == "pass"
    assert result.runs == 1, "复审没跑通就该说没检查过，不能谎称检查过两次"
    assert result.agreed is True


def test_复审提示词不包含第一次的结论():
    """给它看第一次的答案就是在请它同意。那条路在这个仓库失败过六次。"""
    from services import review_consensus

    submitted = _v("pass", basis=["我第一次判通过"])
    adapter = _Adapter([_json("pass")])
    run(
        review_consensus.verify_submission(
            adapter,
            submitted=submitted,
            instructions="按额度审。",
            materials="住宿 480",
            required_inputs=("金额",),
            runs=2,
        )
    )
    # complete 收到的参数形状由适配器决定，所以整体转成字符串再查——
    # 断言的是"第一次的结论有没有漏进提示词"，不依赖参数怎么组织
    blob = str(adapter.calls[0])
    assert "我第一次判通过" not in blob
    assert "住宿 480" in blob
    assert "按额度审" in blob
