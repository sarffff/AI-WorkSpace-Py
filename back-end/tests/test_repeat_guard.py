"""重复调用检测。

轮次上限和结果字符预算都管不到这件事:重复调用每次都是合法调用、都在预算内,
只是拿回来的东西一模一样。这里覆盖的是"第几次开始拦"、"参数顺序不影响判定"、
以及"拦下来之后循环是否真的收敛"——最后这条是它存在的理由,不然只是少跑一次工具。
"""
from __future__ import annotations

from services.tool_runtime import RepeatGuard, ToolStatus


def test_allows_calls_below_limit():
    guard = RepeatGuard(3)

    assert guard.check("search", {"q": "报销"}) is None
    assert guard.check("search", {"q": "报销"}) is None
    assert guard.blocked == 0


def test_blocks_on_nth_call():
    guard = RepeatGuard(3)
    guard.check("search", {"q": "报销"})
    guard.check("search", {"q": "报销"})

    result = guard.check("search", {"q": "报销"})

    assert result is not None
    assert result.status is ToolStatus.REPEATED
    assert guard.blocked == 1
    # 计数要说得准:这是第 3 次,上限 3
    assert "第 3 次" in result.content
    # 纠正说明必须告诉它下一步该干什么,只说"被拦了"它下一轮还会再调
    assert "换检索词" in result.content
    assert "直接作答" in result.content


def test_keeps_counting_after_block():
    """被拦的调用也计数:模型无视纠正继续调时,浪费次数应该接着涨。"""
    guard = RepeatGuard(2)
    guard.check("search", {"q": "x"})
    guard.check("search", {"q": "x"})
    guard.check("search", {"q": "x"})

    assert guard.blocked == 2


def test_argument_order_does_not_matter():
    """``{"a":1,"b":2}`` 和 ``{"b":2,"a":1}`` 是同一次调用,不排序会漏掉一半重复。"""
    guard = RepeatGuard(2)

    assert guard.check("read", {"a": 1, "b": 2}) is None
    assert guard.check("read", {"b": 2, "a": 1}) is not None


def test_different_arguments_are_independent():
    guard = RepeatGuard(2)
    guard.check("search", {"q": "报销"})

    assert guard.check("search", {"q": "差旅"}) is None
    assert guard.check("search", {"q": "报销"}) is not None


def test_different_tools_are_independent():
    guard = RepeatGuard(2)
    guard.check("search", {"q": "x"})

    assert guard.check("read", {"q": "x"}) is None


def test_non_consecutive_repeats_are_caught():
    """A、B、A、B、A 在两个相同查询之间来回摆是同一种病,只看连续完全抓不到。"""
    guard = RepeatGuard(3)
    guard.check("search", {"q": "A"})
    guard.check("search", {"q": "B"})
    guard.check("search", {"q": "A"})
    guard.check("search", {"q": "B"})

    assert guard.check("search", {"q": "A"}) is not None


def test_zero_limit_disables_detection():
    """0 = 关闭检测,退回改动前的行为。这是 no-repeat-guard 变体的依据。"""
    guard = RepeatGuard(0)

    assert not guard.enabled
    for _ in range(10):
        assert guard.check("search", {"q": "x"}) is None
    assert guard.blocked == 0


def test_unhashable_arguments_do_not_crash():
    """参数来自 ``json.loads``,理论上都可序列化,但不该因为一个怪值就炸掉整轮。"""
    guard = RepeatGuard(2)

    assert guard.check("t", {"v": {1, 2}}) is None
    assert guard.check("t", {"v": {1, 2}}) is not None


def test_limit_one_blocks_immediately():
    """极端配置:1 表示一次都不许调。行为要可预测,不能变成"无限次"。"""
    guard = RepeatGuard(1)

    assert guard.check("search", {"q": "x"}) is not None
