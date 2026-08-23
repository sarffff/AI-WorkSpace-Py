"""``keyword_coverage`` 的空白归一化，以及它解决的那个假满分问题。

2026-08-23：原实现是纯子串匹配，于是金标只能断言裸数字（``"5"``），因为
``"5 天"`` 匹配不上模型写的 ``"5天"``。而裸数字会被同篇更长的数字整体包含，
后果是**答错也算满分**——30 条老题里 9 条中招。

这个文件钉住两件事：折叠让排版差异不再影响命中；折叠之后"数字+量词"重新可用，
且能把错答案判成不命中。
"""
from __future__ import annotations

from eval.metrics import keyword_coverage


def test_whitespace_between_number_and_unit_is_ignored():
    """语料写「3 个工作日」，模型写「3个工作日」，应当算命中。"""
    assert keyword_coverage("需要 3个工作日", ["3 个工作日"]) == 1.0
    assert keyword_coverage("需要 3 个工作日", ["3个工作日"]) == 1.0


def test_bare_digit_still_matches_inside_longer_number():
    """裸数字的缺陷本身：这是**现有行为**，不是期望行为。

    留这条断言是为了说明"为什么金标不该再用裸数字"——折叠不解决它，
    只有换关键词能解决。哪天有人想把某条金标改回裸数字，这条会提醒他。
    """
    # 问的是结转 5 天，答的是陪产假 15 天——裸数字 "5" 仍然命中
    assert keyword_coverage("配偶陪产假 15 天", ["5"]) == 1.0


def test_number_plus_unit_rejects_the_wrong_answer():
    """换成「结转 5 天」之后，关于 15 天的答案不再命中。"""
    assert keyword_coverage("配偶陪产假 15 天", ["结转 5 天"]) == 0.0
    assert keyword_coverage("最多结转5天到次年3月31日", ["结转 5 天"]) == 1.0


def test_percent_sign_disambiguates_from_larger_amount():
    """「30%」不会被「300 元」命中，而裸「30」会。"""
    assert keyword_coverage("客户招待 300 元", ["30%"]) == 0.0
    assert keyword_coverage("不超过招待总额的 30%", ["30%"]) == 1.0
    # 对照：裸数字会误命中
    assert keyword_coverage("客户招待 300 元", ["30"]) == 1.0


def test_partial_coverage_is_proportional():
    assert keyword_coverage("只提到 4 小时", ["4 小时", "自动回收"]) == 0.5


def test_case_is_folded():
    assert keyword_coverage("使用 HMAC 签名", ["hmac"]) == 1.0
    assert keyword_coverage("使用 hmac 签名", ["HMAC"]) == 1.0


def test_empty_keywords_is_full_coverage():
    """没有关键词时不该把它算成失败——absent/injection 那类题没有 must_include。"""
    assert keyword_coverage("任意答案", []) == 1.0
