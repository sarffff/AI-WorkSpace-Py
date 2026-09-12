"""``--repeat`` 的合并逻辑。

这个开关存在的理由是**单次跑的分数不可判定**：同配置连跑两次，某条用例能从
2.0 摆到 5.0。这个仓库为此手搭过三次一次性探针，每次都在重新发明同一件事。

所以这里钉的不是"能不能跑两遍"，而是四件会静默错掉的事：

1. **``repeat=1`` 必须与改动前逐字节一致。** 不一致的话历史报告和新报告不可比，
   而"能不能比"正是这个功能要解决的问题。
2. **计数字段取均值，不取和。** ``modelToolCalls`` 是跨任务求和的，按和合并会把
   53 变成 159——每一处读数都错，而且看起来像回归。
3. **数据集描述量不该被平均成小数。** ``tasks: 48`` 三轮之后还得是 48。
4. **逐用例要留每轮原值。** 只留均值就答不出"这条是不稳还是真退化了"，
   而那正是要问的问题。
"""
from __future__ import annotations

import pytest

from eval.agent_runner import (
    _merge_details,
    _merge_summaries,
    _VOLATILE_SPAN,
    volatile_tasks,
)


def _summary(**overrides):
    base = {
        "variant": "fs-skills",
        "description": "固定描述",
        "tasks": 48,
        "turns": 50,
        "taskSuccess": 4.6,
        "modelToolCalls": 53,
        "successByProbe": {"approval": 4.6, "skill": 5.0},
        "unpricedModels": ["glm-4.6v"],
        "turnErrorReasons": ["clarification_never_asked ×4"],
        "toolRecall": None,
    }
    base.update(overrides)
    return base


# ========== 单轮：必须原样穿过 ==========


def test_单轮原样返回连键都不多():
    """``repeat=1`` 不该多出 runs / spread。

    多出来的话历史报告和新报告的形状就不一样了，而 gate.py 与所有读报告的人
    都在按形状读。默认路径必须是零改动。
    """
    one = _summary()
    merged = _merge_summaries([one])
    assert merged is one, "单轮应当原样返回同一个对象，不是拷贝"
    assert "runs" not in merged
    assert "spread" not in merged


def test_单轮的逐用例明细也原样返回():
    details = [{"id": "t1", "success": 5.0}]
    assert _merge_details([details]) is details


# ========== 多轮：计数取均值而不是求和 ==========


def test_计数字段取均值而不是求和():
    """``modelToolCalls`` 是跨任务求和的计数。

    按求和合并的话 --repeat 3 会把 53 变成 159。那个数字每一处读法都错，
    而且它看起来像"模型突然开始疯狂调工具"——一个不存在的回归。
    """
    merged = _merge_summaries(
        [
            _summary(modelToolCalls=53),
            _summary(modelToolCalls=59),
            _summary(modelToolCalls=50),
        ]
    )
    assert merged["modelToolCalls"] == 54.0
    assert merged["spread"]["modelToolCalls"] == {
        "min": 50,
        "max": 59,
        "values": [53, 59, 50],
    }


def test_数据集描述量跨轮相同就原样保留():
    """``tasks`` 三轮都是 48，合并之后还得是整数 48。

    这条不需要一张"哪些字段不该平均"的名单：跨轮完全相同的字段恰好就是数据集
    描述量，而相同值取均值等于它自己。这里钉的是"没被变成 48.0"——
    报告里出现 48.0 个任务会让人以为哪里算错了。
    """
    merged = _merge_summaries([_summary(), _summary(), _summary()])
    assert merged["tasks"] == 48
    assert isinstance(merged["tasks"], int)
    assert "tasks" not in merged["spread"], "没波动的字段不该进 spread"
    assert merged["variant"] == "fs-skills"
    assert merged["runs"] == 3


# ========== None 的含义要保住 ==========


def test_某轮没有可判定样本时只平均有值的那几轮():
    """``None`` 是"这一轮没有可判定的标的"，不是 0。

    用 0 填空会把"没有标的"和"判定为失败"混成同一个数字——这个仓库在
    summarize 里已经为这件事写过一遍理由，合并这一层不能把它抹掉。
    而 values 里要把 None 留着：少了它就看不出这是几轮里的均值。
    """
    merged = _merge_summaries(
        [
            _summary(toolRecall=0.6),
            _summary(toolRecall=None),
            _summary(toolRecall=0.8),
        ]
    )
    assert merged["toolRecall"] == pytest.approx(0.7)
    assert merged["spread"]["toolRecall"]["values"] == [0.6, None, 0.8]
    assert merged["spread"]["toolRecall"]["min"] == 0.6


def test_全轮都是None时保持None():
    merged = _merge_summaries([_summary(), _summary(taskSuccess=4.7)])
    # toolRecall 三轮都是 None → 相同 → 原样保留
    assert merged["toolRecall"] is None


# ========== 字典与列表 ==========


def test_按探针的成功率逐键平均():
    merged = _merge_summaries(
        [
            _summary(successByProbe={"approval": 4.0, "skill": 5.0}),
            _summary(successByProbe={"approval": 5.0, "skill": 5.0}),
        ]
    )
    assert merged["successByProbe"]["approval"] == 4.5
    assert merged["successByProbe"]["skill"] == 5.0


def test_某轮缺席的探针只平均在场的那几轮():
    """一个 probe 可能在某轮整体裁判失败而缺席。

    把缺席当 0 会凭空拉低那个 probe 的分数，而它其实没有样本。
    """
    merged = _merge_summaries(
        [
            _summary(successByProbe={"approval": 4.0}),
            _summary(successByProbe={"approval": 5.0, "skill": 3.0}),
        ]
    )
    assert merged["successByProbe"]["approval"] == 4.5
    assert merged["successByProbe"]["skill"] == 3.0


def test_列表按出现顺序去重合并():
    merged = _merge_summaries(
        [
            _summary(unpricedModels=["glm-4.6v"]),
            _summary(unpricedModels=["glm-4.6v", "bge-m3"]),
        ]
    )
    assert merged["unpricedModels"] == ["glm-4.6v", "bge-m3"]


# ========== 逐用例：每轮原值必须留下 ==========


def test_逐用例留下每轮分数与最差那轮的轨迹():
    """轨迹留**分最低那一轮**的，不是第一轮的。

    留全部会让报告体积 ×N（48 任务的 JSON 已经四千多行）；留第一轮则有一半
    概率留错——排查波动时想读的恰恰是失败那次的工具序列。
    """
    runs = [
        [{"id": "t1", "probe": "skill", "success": 5.0, "grounded": 5.0,
          "judgeReason": "好", "turns": ["第一轮轨迹"]}],
        [{"id": "t1", "probe": "skill", "success": 3.0, "grounded": 4.0,
          "judgeReason": "差", "turns": ["第二轮轨迹"]}],
    ]
    merged = _merge_details(runs)
    assert len(merged) == 1
    row = merged[0]
    assert row["successRuns"] == [5.0, 3.0]
    assert row["success"] == 4.0
    assert row["groundedRuns"] == [5.0, 4.0]
    assert row["judgeReasonRuns"] == ["好", "差"]
    # 轨迹来自第 2 轮（分最低那轮）
    assert row["turns"] == ["第二轮轨迹"]
    assert row["traceFromRun"] == 2
    assert row["runs"] == 2


def test_用例顺序按首次出现保持不变():
    """报告是给人按顺序读的，合并不该重排。"""
    runs = [
        [{"id": "a", "success": 5.0}, {"id": "b", "success": 5.0}],
        [{"id": "b", "success": 3.0}, {"id": "a", "success": 3.0}],
    ]
    assert [row["id"] for row in _merge_details(runs)] == ["a", "b"]


# ========== 波动判定 ==========


def test_波动阈值挑出摆动的用例并按幅度排序():
    details = [
        {"id": "steady", "probe": "skill", "successRuns": [5.0, 5.0, 5.0]},
        {"id": "wobbly", "probe": "clarification", "successRuns": [5.0, 3.0, 5.0]},
        {"id": "worst", "probe": "absent", "successRuns": [1.0, 5.0, 3.0]},
    ]
    rows = volatile_tasks(details)
    assert [row["id"] for row in rows] == ["worst", "wobbly"]
    assert rows[0]["span"] == 4.0
    assert rows[1]["span"] == _VOLATILE_SPAN


def test_单轮不产生波动行():
    """只跑了一轮就没有"跨轮"可言，这一节该是空的而不是报一堆 span=0。"""
    assert volatile_tasks([{"id": "t1", "successRuns": [5.0]}]) == []


def test_裁判失败的轮次不算进波动():
    """``None`` 不是低分。把它当 0 会让每条偶发裁判失败都变成一次"剧烈波动"。"""
    rows = volatile_tasks([{"id": "t1", "successRuns": [5.0, None, 5.0]}])
    assert rows == []
