"""按用户的用量闸门：请求频率、成本、token。

改动之前 ``/chats/completions/stream`` 上没有任何上界。``@limiter.limit`` 只挂在
auth 的登录与注册上，而这条路径是全项目最贵的一条：最多 ``AGENT_MAX_TOOL_ROUNDS``
轮模型调用，每轮可能带 ``web_search`` 与向量化。成本那边 ``telemetry`` 一直在算、
一直写进 ``trace_spans.cost``，但没有任何一处**因为成本拒绝执行**——测量做完了，
处置没有。两条合起来就是：一个脚本等于无上界地花钱。

## 为什么不直接用 slowapi

项目里已经有 ``rate_limit.limiter``，但它在这里不合适，两个原因：

1. **它按 IP 限流**（``key_func=get_remote_address``）。这是个已登录端点，正确的
   键是用户 id。按 IP 限会把同一间办公室的人算成一个，也拦不住换 IP 的脚本。
2. **参数名撞了。** slowapi 要求被装饰的函数有一个名为 ``request`` 的
   ``starlette.Request`` 参数，而 ``stream_completions`` 的 ``request`` 已经是
   ``ChatRequest`` 请求体。

而且成本配额是 slowapi 根本表达不了的东西——它只会数请求次数。所以这里写成一个
依赖：键是用户 id，三个上界在同一处判定，只多一次查询。

## 三个上界分别防什么

- **频率**防滥用。进程内计数，**每个请求都算，不管有没有花到钱**。早早失败的请求
  也必须算，否则打空请求的脚本永远撞不到上限。
- **成本**防超支。读 ``trace_spans.cost``，也就是真实记账。
- **token** 是成本的兜底。价目表里没有的模型 ``cost`` 是 ``NULL``（见
  ``pricing`` 模块：宁可承认不知道，也不给假数字），所以只卡成本的话，换一个
  未定价的模型就绕过去了。token 永远有记录——要么提供商实测，要么本地估算。

## 已知的不精确，以及为什么可以接受

**频率计数在进程内。** 多 worker 部署下每个进程各有一份，实际生效的上限是
``worker 数 × USAGE_RATE_MAX_REQUESTS``。要精确得放进 Redis，而项目现在没有
Redis 依赖，为这一个功能引入一个新的运行时组件不划算。这个偏差的方向是安全的
（比配置的更宽松，不会误杀），而真正防超支的是成本与 token 那两道——它们读数据库，
天然跨进程一致。

**成本与 token 是滞后的。** 它们统计的是**已经记账**的用量，而当前这次请求还没
发生。所以一次请求最多可以超出上限一个回合的量。要做到不超出一分钱得先预扣、
再按实际结算，那需要一张预留表和一条对账路径；在「防跑飞」这个目标下，滞后一个
回合是可接受的——它拦住的是第 N+1 次，而不是让第 N 次刚好停在线上。

**窗口是滑动的，不按自然日对齐。** 按自然日对齐会在午夜给出一整份新配额，于是
「卡住了就等到零点」变成一种可行的绕过方式。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from config import settings
from models import TraceSpan
from services.clock import naive_now

logger = logging.getLogger("usage_guard")


@dataclass(frozen=True, slots=True)
class Rejection:
    """一次拒绝。``retry_after`` 是建议的重试等待秒数，没有把握时为 None。"""

    # rate / cost / tokens —— 哪一道闸门拦下的。进日志与响应头用
    kind: str
    message: str
    retry_after: int | None = None


class _RateCounter:
    """按用户的滑动窗口请求计数。

    用 ``deque`` 存时间戳而不是「计数 + 窗口起点」：后者是固定窗口，在窗口边界上
    可以放过两倍的量（窗口末尾打满一次、下个窗口开头再打满一次）。滑动窗口没有
    这个缺口，代价是每个用户要存最多 ``limit`` 个浮点数。

    ``time.monotonic()`` 而不是墙上时间：这是个纯粹的时间间隔判断，不该被 NTP
    校准或夏令时改动影响。

    ``clock`` 可注入是为了测试：滑动窗口与固定窗口的区别**只在时间推进之后才显现**
    （固定窗口在翻页那一刻整份重置，滑动窗口是一个一个地放出位置）。不注入时钟就
    只能靠 ``sleep`` 去测，而那意味着用真实秒数换一个本来可以确定的断言。
    """

    # 每多少次 ``hit`` 顺带清一遍窗口外的条目。``_hits`` 会随「历史上出现过的
    # 用户数」单调增长——每个用户的 deque 有上限，所以泄漏很慢，但它确实是泄漏。
    #
    # 稀疏地清而不是每次都清：清理要遍历整个字典，而放行是热路径。清理放在
    # ``hit`` 里面是因为它已经持有锁了，在外面单独调等于再抢一次锁。
    _PRUNE_EVERY = 64

    __slots__ = ("_hits", "_lock", "_clock", "_since_prune")

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._since_prune = 0
        self._hits: dict[str, deque[float]] = {}
        # FastAPI 的依赖可能跑在不同的线程池线程上（同步依赖会被丢进
        # threadpool），所以这里的读改写必须上锁。不加锁的表现是偶发的计数丢失，
        # 而且只在并发下出现——最难查的那一类。
        self._lock = threading.Lock()

    def hit(self, key: str, *, window_seconds: float, limit: int) -> int | None:
        """登记一次请求。放行返回 None，该拦时返回建议等待的秒数。

        计数在**判定之后**才加，所以 limit=20 的语义是「第 21 次起拦」。
        被拦的请求**不计数**——这与 ``RepeatGuard`` 的取舍相反，理由也相反：那里
        要的是「真实浪费次数」这个埋点，而这里如果把被拦的请求也算进去，一个持续
        重试的客户端会把自己的窗口无限往后推，等于变成永久封禁。
        """
        if limit <= 0 or window_seconds <= 0:
            return None
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            self._since_prune += 1
            if self._since_prune >= self._PRUNE_EVERY:
                self._since_prune = 0
                self._prune_locked(cutoff)
            hits = self._hits.get(key)
            if hits is None:
                hits = deque()
                self._hits[key] = hits
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                # 最早那一次滑出窗口，就又有一个位置了
                wait = hits[0] + window_seconds - now
                return max(1, int(wait) + 1)
            hits.append(now)
            return None

    def reset(self, key: str | None = None) -> None:
        """清空计数。``key`` 为 None 时全清。测试用。"""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def prune(self, *, window_seconds: float) -> int:
        """丢掉窗口内没有任何请求的用户，返回清掉的条目数。

        不清理的话 ``_hits`` 会随「历史上出现过的用户数」单调增长。每个用户的
        deque 有上限，所以泄漏很慢，但它确实是泄漏。
        """
        if window_seconds <= 0:
            return 0
        cutoff = self._clock() - window_seconds
        with self._lock:
            return self._prune_locked(cutoff)

    def _prune_locked(self, cutoff: float) -> int:
        """清理本体。调用方必须已持有 ``_lock``。"""
        stale = [
            key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff
        ]
        for key in stale:
            del self._hits[key]
        return len(stale)


_rate_counter = _RateCounter()


def _quota_window_start():
    hours = max(0.0, settings.USAGE_QUOTA_WINDOW_HOURS)
    # 与 trace_spans.started_at 同一个时钟（应用时区的 naive 值），
    # 用 UTC 去比会在东八区下错开 8 小时，窗口统计静默错位。
    return naive_now() - timedelta(hours=hours)


@dataclass(frozen=True, slots=True)
class Spent:
    """窗口内已记账的用量。``costs`` 按币种分开，不做汇率换算。"""

    tokens: int
    costs: dict[str, Decimal]


def spent_in_window(db: Session, user_id: str) -> Spent:
    """读窗口内这个用户已经花掉的 token 与成本。

    一次查询拿两样，走 ``ix_trace_spans_user_started`` 这个既有索引。
    ``coalesce`` 是必须的：没有任何 span 时 ``sum`` 返回 NULL 而不是 0。
    """
    since = _quota_window_start()
    tokens_row = (
        db.query(
            func.coalesce(func.sum(TraceSpan.prompt_tokens), 0),
            func.coalesce(func.sum(TraceSpan.completion_tokens), 0),
        )
        .filter(TraceSpan.user_id == user_id, TraceSpan.started_at >= since)
        .one()
    )
    tokens = int(tokens_row[0] or 0) + int(tokens_row[1] or 0)

    cost_rows = (
        db.query(TraceSpan.currency, func.sum(TraceSpan.cost))
        .filter(
            TraceSpan.user_id == user_id,
            TraceSpan.started_at >= since,
            TraceSpan.cost.isnot(None),
        )
        .group_by(TraceSpan.currency)
        .all()
    )
    costs: dict[str, Decimal] = {}
    for currency, amount in cost_rows:
        if amount is None:
            continue
        # currency 允许为 NULL（价目表没写 currency 时），归到一个显式的桶里，
        # 而不是让它变成字典里的 None 键——那会在格式化消息时打印成 "None"。
        costs[str(currency or "?")] = Decimal(str(amount))
    return Spent(tokens=tokens, costs=costs)


def _format_window() -> str:
    hours = settings.USAGE_QUOTA_WINDOW_HOURS
    if hours >= 1 and float(hours).is_integer():
        return f"{int(hours)} 小时"
    return f"{hours * 60:.0f} 分钟"


def check(db: Session, user_id: str) -> Rejection | None:
    """三道闸门。放行返回 None，该拦时返回 ``Rejection``。

    顺序是刻意的：**频率在最前面，而且它不查库。** 被限流的请求通常是脚本打出来
    的，这种情况下每个请求都去查一次聚合等于把数据库也一起打满——限流本身成了
    放大器。所以先用进程内计数把量挡掉，再去读账。
    """
    if not settings.USAGE_GUARD_ENABLED:
        return None

    window_seconds = max(0.0, settings.USAGE_RATE_WINDOW_MINUTES) * 60
    retry_after = _rate_counter.hit(
        user_id,
        window_seconds=window_seconds,
        limit=settings.USAGE_RATE_MAX_REQUESTS,
    )
    if retry_after is not None:
        return Rejection(
            kind="rate",
            message=(
                f"请求过于频繁：{_format_rate_window()}内最多 "
                f"{settings.USAGE_RATE_MAX_REQUESTS} 次对话请求。"
                f"请等待约 {retry_after} 秒后重试。"
            ),
            retry_after=retry_after,
        )

    max_cost = settings.USAGE_QUOTA_MAX_COST
    max_tokens = settings.USAGE_QUOTA_MAX_TOKENS
    if max_cost <= 0 and max_tokens <= 0:
        return None

    try:
        spent = spent_in_window(db, user_id)
    except Exception:
        # 配额查询失败不该把对话也一起挡掉：这是个防超支的护栏，不是鉴权。
        # 记一条日志并放行，比让埋点表的一次抖动变成全站不可用要好。
        logger.exception("usage quota lookup failed for user; allowing the request")
        return None

    if max_cost > 0:
        limit = Decimal(str(max_cost))
        for currency, amount in sorted(spent.costs.items()):
            if amount >= limit:
                return Rejection(
                    kind="cost",
                    message=(
                        f"已达用量上限：最近 {_format_window()}内已消耗 "
                        f"{amount:.4f} {currency}，上限为 {max_cost:.4f}。"
                        "请稍后再试或联系管理员调整配额。"
                    ),
                )

    if max_tokens > 0 and spent.tokens >= max_tokens:
        return Rejection(
            kind="tokens",
            message=(
                f"已达用量上限：最近 {_format_window()}内已消耗 "
                f"{spent.tokens} tokens，上限为 {max_tokens}。"
                "请稍后再试或联系管理员调整配额。"
            ),
        )

    return None


def _format_rate_window() -> str:
    minutes = settings.USAGE_RATE_WINDOW_MINUTES
    if minutes >= 1 and float(minutes).is_integer():
        return f"{int(minutes)} 分钟"
    return f"{minutes * 60:.0f} 秒"


def reset_rate_counter(user_id: str | None = None) -> None:
    """清空频率计数。测试与运维用。"""
    _rate_counter.reset(user_id)


def prune_rate_counter() -> int:
    """丢掉窗口外的用户条目，返回清掉的条数。"""
    return _rate_counter.prune(
        window_seconds=max(0.0, settings.USAGE_RATE_WINDOW_MINUTES) * 60
    )


__all__ = [
    "Rejection",
    "Spent",
    "check",
    "prune_rate_counter",
    "reset_rate_counter",
    "spent_in_window",
]
