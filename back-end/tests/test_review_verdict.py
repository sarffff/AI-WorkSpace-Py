"""审核结论的结构：缺前提就下不了结论。

这个文件钉的是**机制**，不是措辞。背景值得写清楚，因为它决定了为什么这里是
validator 而不是提示词：

这个仓库有六次记录说明"请求模型多做一件事"不成立——记忆 ×3、``ask_user`` ×2、
skill ×1，全部失败；唯一成功的一次是**撤掉一句邀请**（工具失败时别再邀请模型编
数字，编造率 4/8 → 0/8）。而 ``expense-review`` 的正文里早就写着"缺一个就问用户"，
实测 4 条澄清用例 ``clarificationAsked`` 是 **0**。措辞这条路走到头了。

所以判据搬进结构：``ReviewVerdict.inputs`` 逐项对齐 SOP 声明的前置材料，
``found=false`` 存在时 ``verdict`` 只能是 ``needs_human``，由 validator 强制。
"漏了一项"于是从判断题（模型该不该主动问）变成填空题（这一项在不在清单里）。

## 为什么"结论必须可机械比较"不是防御性设计

2026-09-12 实测：同一条用例跑三轮，**工具调用序列逐次相同**，而裁判分是 5/5/3
（``absent-byod``）与 5/3/5（``multi-key-leak-response``）。对照组 ``fs-read-single``
三轮全 5.0。也就是说摆动在**结论层**，而且集中在"该不该给出确定结论"这一类。
温度是 0.0（``eval/agent_runner.py``），所以这不是配置问题。

自由文本比不出这种差别：措辞变了但结论相同、措辞相同但金额差一位，两种都会误判。
比 ``verdict`` 字段就能。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from services import skill_library, skill_service
from services.structured import InputCheck, ReviewVerdict


def _inputs(**found: bool) -> list[InputCheck]:
    """按 name=found 造一批核对项。value 跟着 found 走，省得每条都写。"""
    return [
        InputCheck(name=name, found=ok, value="有" if ok else None)
        for name, ok in found.items()
    ]


# ========== validator：缺料就只能转人工 ==========


def test_缺一项前置材料时不许给通过():
    """这是整个设计的支点：它必须是构造失败，不是一句提示。

    失败方式要看清楚——不是"模型被劝住了"，是这份结论**根本构造不出来**。
    ``request_structured`` 会把 ValidationError 原文回灌重试，重试仍失败则整份
    结论作废，而那是正确的处置：一份"缺着材料却说通过"的结论比没有结论危险。
    """
    with pytest.raises(ValidationError) as caught:
        ReviewVerdict(
            inputs=_inputs(出差城市=True, 发生日期=True, 费用类别=True, 凭证=False),
            basis=["按额度标准第 3 条"],
            verdict="pass",
            sop_name="expense-review",
            sop_version=1,
        )
    # 报错要点名**是哪一项**缺了：模型据此才知道该去补什么，
    # 只说"参数不合法"它无从下手（同 _retry_message 里回灌原文那条理由）
    assert "凭证" in str(caught.value)


def test_缺料时也不许给不通过():
    """``reject`` 同样不行——这一条容易被漏掉。

    "材料不全所以拒了"听起来像个负责的结论，实际上它和 pass 犯的是同一个错：
    在不知道的情况下给了一个确定答案。用户拿着这个 reject 去申诉，而真实情况
    是"还没审"。
    """
    with pytest.raises(ValidationError):
        ReviewVerdict(
            inputs=_inputs(出差城市=True, 凭证=False),
            verdict="reject",
            sop_name="expense-review",
            sop_version=1,
        )


def test_缺料时转人工是合法的():
    """反向：needs_human 必须能构造出来。

    不测这条的话，把 validator 写成"缺料就一律拒绝构造"也能让上面两条通过——
    那时这个功能实际上是坏的：它连正确的出口都堵住了。
    """
    verdict = ReviewVerdict(
        inputs=_inputs(出差城市=True, 凭证=False),
        basis=["凭证缺失，按流程需上级判断"],
        verdict="needs_human",
        sop_name="expense-review",
        sop_version=2,
    )
    assert verdict.verdict == "needs_human"
    assert [item.name for item in verdict.inputs if not item.found] == ["凭证"]


def test_材料齐全时可以给确定结论():
    """反向：齐全时 pass / reject 都不该被拦。

    validator 只管"缺料"这一件事。它要是顺手把齐全的情形也拦了，
    表现是这个产品永远只输出 needs_human——看起来很谨慎，实际毫无用处。
    """
    for decided in ("pass", "reject"):
        verdict = ReviewVerdict(
            inputs=_inputs(出差城市=True, 发生日期=True),
            basis=["按额度标准第 3 条：一线城市住宿上限 500"],
            verdict=decided,
            sop_name="expense-review",
            sop_version=1,
        )
        assert verdict.verdict == decided


def test_没有前置材料要求的SOP不受影响():
    """写作指导类的 skill 没有 required_inputs，inputs 就是空的。

    空列表不能被当成"缺料"——那会让所有非审核型 skill 都只能输出 needs_human。
    """
    verdict = ReviewVerdict(
        inputs=[],
        verdict="pass",
        sop_name="quarterly-report",
        sop_version=1,
    )
    assert verdict.verdict == "pass"


# ========== 字段顺序：先核料、再写依据、最后落结论 ==========


def test_字段顺序把结论排在推理之后():
    """同 ``AbstentionVerdict`` 那条教训：bool 排在推理之前时，它在理由写完之前
    就落定了，而模型写完理由不会回头改。

    钉的是 schema 里键的**声明顺序**——生成 JSON 时模型按这个顺序逐个填，
    所以顺序本身就是约束。有人为了"好看"把 verdict 提到最前面时，这条会红。
    """
    keys = list(ReviewVerdict.model_fields)
    assert keys.index("inputs") < keys.index("verdict")
    assert keys.index("basis") < keys.index("verdict")
    # InputCheck 内部同理：先抄值，再判在不在
    assert list(InputCheck.model_fields).index("value") < list(
        InputCheck.model_fields
    ).index("found")


# ========== required_inputs 的解析 ==========


def test_中英文逗号都能分开():
    """这一行是人在界面上手写的，中文输入法下打出「，」是常态。

    不收全角逗号的后果不报错：四项被解析成一项，表现是"这份 SOP 只要求一样东西"
    —— 审核安静地松了一档。
    """
    assert skill_library.parse_required_inputs("金额, 凭证, 日期") == (
        "金额",
        "凭证",
        "日期",
    )
    assert skill_library.parse_required_inputs("金额，凭证，日期") == (
        "金额",
        "凭证",
        "日期",
    )
    assert skill_library.parse_required_inputs("金额, 凭证，日期") == (
        "金额",
        "凭证",
        "日期",
    )


def test_空与缺省都是空tuple():
    """没有前置材料要求是合法状态（写作指导类），不是错误。"""
    assert skill_library.parse_required_inputs(None) == ()
    assert skill_library.parse_required_inputs("") == ()
    assert skill_library.parse_required_inputs("   ") == ()
    # 多余的逗号不该产出空项：空字符串项会变成一个永远 found=false 的槽位，
    # 于是这份 SOP 永远只能输出 needs_human
    assert skill_library.parse_required_inputs("金额,,凭证,") == ("金额", "凭证")


# ========== 内置 skill 真的带上了声明 ==========


def test_expense_review_声明了前置材料():
    """正文里"缺一个就问用户"那句话必须有对应的结构声明。

    两者不一致是最坏的情形：正文说要四项、结构里只列两项，于是漏掉的那两项
    既没人问、也不在核对清单里——比两处都没有更难发现。
    """
    skills = skill_library.builtin()
    assert "expense-review" in skills
    required = skills["expense-review"].required_inputs
    assert required, "expense-review 是审核型 SOP，必须声明前置材料"
    # 正文第 1 节点名了城市、日期、费用类别、凭证四类
    joined = "".join(required)
    for expected in ("城市", "日期", "类别", "凭证"):
        assert expected in joined, f"正文里要求了{expected}，声明里没有"


# ========== SOP 版本号 ==========
#
# 结论引用 sop_version，所以这个号的两种错法后果相反：
#   不涨 → 改一次 SOP，历史结论的依据全部指向一份不存在的文本
#   乱涨 → 号大到没人看，"版本变了"这个信号失效


def _upsert(db, **over):
    payload = {
        "name": "expense",
        "description": "审报销单",
        "instructions": "第一步：核对额度。",
    }
    payload.update(over)
    return skill_service.upsert(db, "w1", **payload)


def test_新建的skill从第1版开始(db_real):
    """不是 0——结论里引用「第 0 版」读起来像"还没有版本"。"""
    assert _upsert(db_real).version == 1


def test_改正文让版本号加一(db_real):
    _upsert(db_real)
    assert _upsert(db_real, instructions="第一步：先看凭证。").version == 2


def test_改描述也算一次改动(db_real):
    """description 是模型选 skill 的唯一依据，改它就是改了行为。"""
    _upsert(db_real)
    assert _upsert(db_real, description="审报销单（含例外流程）").version == 2


def test_改前置材料声明也算(db_real):
    """required_inputs 直接决定 ReviewVerdict 有几个必填槽位，也就是直接决定
    审核多严。改了它而版本号不动，等于悄悄放宽了标准。"""
    _upsert(db_real, required_inputs="金额")
    assert _upsert(db_real, required_inputs="金额, 凭证").version == 2


def test_一个字没改时版本号不动(db_real):
    """钉的是"先判再写"那一步。

    写完再比就永远相等，那是这类"变了才 +1"最容易踩的一脚——而它的表现是
    版本号**永不增长**，正好是不涨那一侧的后果。
    """
    _upsert(db_real)
    assert _upsert(db_real).version == 1
    assert _upsert(db_real).version == 1


def test_只切enabled不涨版本(db_real):
    """停用再启用不是改规程。每次保存都 +1 的话这个号很快没人看。"""
    _upsert(db_real)
    assert _upsert(db_real, enabled=False).version == 1
    assert _upsert(db_real, enabled=True).version == 1


def test_逗号写法变了不算改动(db_real):
    """存的是归一化之后的串，所以「金额，凭证」改成「金额, 凭证」不该涨版本。

    不归一化就存的话，中文输入法切一次就白涨一个号，而规程一个字没变。
    """
    first = _upsert(db_real, required_inputs="金额，凭证")
    assert first.required_inputs == "金额, 凭证"
    assert _upsert(db_real, required_inputs="金额, 凭证").version == 1
