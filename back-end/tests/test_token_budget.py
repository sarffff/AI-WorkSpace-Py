"""token 计数与历史预算。"""
from __future__ import annotations

from services.token_budget import (
    HeuristicTokenCounter,
    HistoryMessage,
    count_message_tokens,
    get_token_counter,
    plan_history,
)


def _messages(count: int, filler: str = "x" * 40) -> list[HistoryMessage]:
    return [HistoryMessage(str(index), "user", filler) for index in range(count)]


def test_cjk_counts_roughly_one_token_per_char():
    counter = HeuristicTokenCounter()

    assert counter.count("中文预算是多少") == 7
    assert counter.count("") == 0


def test_latin_counts_about_four_chars_per_token():
    counter = HeuristicTokenCounter()

    assert counter.count("hello world foo") == 4


def test_mixed_text_sums_both_scripts():
    counter = HeuristicTokenCounter()

    # 2 个中文字 + 12 个非中文字符(含空格) -> 2 + ceil(12/4)
    assert counter.count("mixed 中文 abcdefg") == 2 + 4


def test_unknown_counter_kind_falls_back_to_heuristic():
    assert isinstance(get_token_counter("nope"), HeuristicTokenCounter)


def test_message_overhead_is_counted():
    counter = HeuristicTokenCounter()
    message = {"role": "user", "content": "你好"}

    # 2(内容) + 1(role "user" -> ceil(4/4)) + 4(固定开销)
    assert count_message_tokens(message, counter) == 7


def test_plan_keeps_newest_messages_within_budget():
    counter = HeuristicTokenCounter()
    plan = plan_history(_messages(5), counter=counter, budget_tokens=32)

    assert [message.id for message in plan.kept] == ["3", "4"]
    assert [message.id for message in plan.dropped] == ["0", "1", "2"]
    assert plan.kept_tokens <= 32
    assert plan.overflowed


def test_plan_keeps_everything_when_budget_is_large():
    plan = plan_history(
        _messages(5), counter=HeuristicTokenCounter(), budget_tokens=100_000
    )

    assert len(plan.kept) == 5
    assert plan.dropped == []
    assert not plan.overflowed


def test_plan_never_splits_a_single_message():
    """预算装不下最新一条时整条进 dropped，不产生半句话的上下文。"""
    plan = plan_history(
        _messages(3), counter=HeuristicTokenCounter(), budget_tokens=1
    )

    assert plan.kept == []
    assert len(plan.dropped) == 3


def test_zero_budget_drops_all_history():
    plan = plan_history(
        _messages(2), counter=HeuristicTokenCounter(), budget_tokens=0
    )

    assert plan.kept == []
    assert len(plan.dropped) == 2


def test_long_messages_are_dropped_earlier_than_short_ones():
    """同样 5 条消息，内容变长后能保留的条数必须变少——这正是按条数截断的盲点。"""
    counter = HeuristicTokenCounter()
    short = plan_history(_messages(5, "abcd"), counter=counter, budget_tokens=40)
    long = plan_history(_messages(5, "x" * 200), counter=counter, budget_tokens=40)

    assert len(short.kept) > len(long.kept)
