"""应用统一时钟。

业务表的 ``created_at``/``updated_at`` 用的是 ``server_default=func.now()``,
也就是 **数据库服务器的墙上时间**。如果应用侧再用 ``datetime.utcnow()`` 去写
同一批列, 同一张表里就会混进两个时区的值。

更隐蔽的一处是埋点: ``trace_spans.started_at`` 由应用写入, 而消息时间由数据库
默认值写入。两边差一个时区偏移时, 「这条消息」和「这条消息的 trace」在界面上
会错开 8 小时, ``/metrics/usage?days=1`` 统计的「今天」也和用户理解的今天对不上。

所以应用侧只保留这一个时钟, 并且默认与数据库服务器所在时区一致
(``APP_TZ_OFFSET_HOURS``, 默认东八区)。数据库部署在 UTC 机器上时把它改成 0,
不要在调用点各自决定用哪个时区。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import settings


def app_timezone() -> timezone:
    """应用时区(与数据库服务器墙上时间对齐)。"""
    return timezone(timedelta(hours=settings.APP_TZ_OFFSET_HOURS))


def now() -> datetime:
    """带时区标记的当前时间, 用于计算与对外序列化。"""
    return datetime.now(app_timezone())


def naive_now() -> datetime:
    """去掉时区标记的当前时间, 用于写入 naive ``DATETIME`` 列。

    MySQL 的 ``DATETIME`` 不存时区, 与 ``func.now()`` 的语义保持一致:
    存的是本地墙上时间。
    """
    return now().replace(tzinfo=None)


def to_naive(value: datetime) -> datetime:
    """把任意时间归一到应用时区的 naive 形式, 便于和库里的值直接比较。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(app_timezone()).replace(tzinfo=None)
