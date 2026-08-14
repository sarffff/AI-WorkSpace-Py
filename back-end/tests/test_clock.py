"""应用时钟的行为测试。

时钟本身没什么逻辑，值得测的是那条不变式：应用写入的时间必须和数据库
``func.now()`` 用同一个墙上时钟。这里用配置偏移来间接验证——偏移改了，
应用时间必须跟着走，而不是硬编码成 UTC 或 UTC+8。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import settings
from services import clock


def test_naive_now_follows_configured_offset(monkeypatch):
    monkeypatch.setattr(settings, "APP_TZ_OFFSET_HOURS", 0)
    utc_based = clock.naive_now()
    monkeypatch.setattr(settings, "APP_TZ_OFFSET_HOURS", 8)
    beijing_based = clock.naive_now()

    # 两次调用之间的真实耗时远小于 1 分钟，差值应当就是 8 小时
    delta = beijing_based - utc_based
    assert timedelta(hours=8) - timedelta(minutes=1) < delta < timedelta(hours=8, minutes=1)


def test_naive_now_has_no_tzinfo(monkeypatch):
    """naive DATETIME 列拒绝 aware 值，这里必须已经剥掉时区。"""
    monkeypatch.setattr(settings, "APP_TZ_OFFSET_HOURS", 8)
    assert clock.naive_now().tzinfo is None
    assert clock.now().tzinfo is not None


def test_to_naive_converts_instead_of_truncating(monkeypatch):
    """aware 值要先换算到应用时区再剥时区，直接 replace 会平移出 8 小时误差。"""
    monkeypatch.setattr(settings, "APP_TZ_OFFSET_HOURS", 8)
    utc_noon = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    assert clock.to_naive(utc_noon) == datetime(2026, 8, 13, 20, 0)


def test_to_naive_passes_through_naive_values(monkeypatch):
    monkeypatch.setattr(settings, "APP_TZ_OFFSET_HOURS", 8)
    value = datetime(2026, 8, 13, 20, 0)

    assert clock.to_naive(value) == value
