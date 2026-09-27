"""出厂配置下,花钱这件事有没有上界。

单独一个文件而不是塞进 ``test_usage_guard``:那边所有用例都用 ``guard_on``
fixture,而它把三项上限**显式钉成 0**(那是对的——每条用例自己设自己要测的那一项)。
于是"出厂默认是多少"在那个文件里恰好测不到,而这正是 2026-09-12 之前的真问题:

``usage_guard`` 写得完全正确、测得也很全,但 ``USAGE_QUOTA_MAX_COST`` 与
``USAGE_QUOTA_MAX_TOKENS`` 都默认 0,而 ``check()`` 里两个都是 0 就直接 return。
也就是说**默认配置下没有任何花钱的上界**,只有 20 次/分钟的频率限制——换算下来
是每天 28,800 次请求。机制建对了,只是没通电。
"""
from __future__ import annotations

from config import Settings


def test_出厂就有token上界():
    """0 = 不限。默认给 0 等于默认不设防。"""
    assert Settings().USAGE_QUOTA_MAX_TOKENS > 0


def test_闸门默认开着():
    assert Settings().USAGE_GUARD_ENABLED is True


def test_token上界够一天很重的正常使用():
    """量出来的:2026-09-12 那轮全量,单回合平均 7,642 token、最贵 16,165。

    下界按"200 个平均回合"定(≈153 万),那已经是很重的一天。低于它意味着真人
    正常用一天就会被拦——那时用户会去把闸门关掉,于是防线归零。

    上界按"一万个平均回合"定。高到那个程度就不再是闸门,只是个不会触发的数字。
    """
    limit = Settings().USAGE_QUOTA_MAX_TOKENS
    avg_turn_tokens = 7_642
    assert limit >= avg_turn_tokens * 200
    assert limit <= avg_turn_tokens * 10_000


def test_成本上界仍然是零而且这是刻意的():
    """成本闸门读 trace_spans.cost,而生产模型 glm-4.6v 没有价目,cost 是 NULL。

    给它配个非零值只会产生一个**永不触发**的闸门,而那比明摆着关掉更糟——
    它会让人以为超支已经被管住了。token 那一项是这种情况下的兜底
    (token 永远有记录,要么提供商实测要么本地估算)。

    这条用例是个**提醒**而不是约束:补上价目表之后,它会红,那时把默认值一起给上。
    """
    assert Settings().USAGE_QUOTA_MAX_COST == 0.0
