"""services/egress.py：SSRF 拦截 + 出口白名单。

用字面 IP 的 URL 测，``getaddrinfo`` 对数字 IP 直接返回、不联网；域名用例都靠
白名单短路（is_allowed 命中即跳过 DNS），所以整个文件不发任何真实网络请求。
异步的 ``check_url`` 用 ``asyncio.run`` 驱动，不依赖 pytest-asyncio。
"""
import asyncio

import pytest

from config import settings
from services import egress
from services.egress import EgressBlocked


def _check(url: str) -> None:
    asyncio.run(egress.check_url(url))


@pytest.fixture(autouse=True)
def _baseline(monkeypatch):
    # 每个用例从"SSRF 开、白名单空"这个默认基线起
    monkeypatch.setattr(settings, "EGRESS_BLOCK_PRIVATE_IPS", True)
    monkeypatch.setattr(settings, "EGRESS_ALLOWLIST", "")


def test_ip_blocked_pure():
    for blocked in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "192.168.1.1", "::1"):
        assert egress._ip_blocked(blocked), blocked
    assert not egress._ip_blocked("8.8.8.8")
    assert not egress._ip_blocked("not-an-ip")


def test_rejects_non_http_scheme():
    for url in ("ftp://example.com/x", "file:///etc/passwd"):
        with pytest.raises(EgressBlocked):
            _check(url)


def test_rejects_missing_host():
    with pytest.raises(EgressBlocked):
        _check("http://")


def test_blocks_private_and_metadata_ips():
    for url in (
        "http://127.0.0.1/",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest/meta-data/",  # 云元数据
        "http://[::1]:8080/",
    ):
        with pytest.raises(EgressBlocked):
            _check(url)


def test_allows_public_ip_when_allowlist_empty():
    _check("http://8.8.8.8/")  # 不抛即通过


def test_private_ip_allowed_when_block_disabled(monkeypatch):
    monkeypatch.setattr(settings, "EGRESS_BLOCK_PRIVATE_IPS", False)
    _check("http://10.0.0.5/")


def test_allowlist_restricts_to_listed_hosts(monkeypatch):
    monkeypatch.setattr(settings, "EGRESS_ALLOWLIST", "docs.python.org")
    # 公网但不在白名单 → 拒
    with pytest.raises(EgressBlocked):
        _check("http://8.8.8.8/")


def test_allowlist_wildcard_matches_domain_and_subdomain(monkeypatch):
    monkeypatch.setattr(settings, "EGRESS_ALLOWLIST", "*.example.com")
    _check("http://api.example.com/x")  # 子域命中
    _check("http://example.com/x")  # 裸域也命中


def test_operator_endpoint_auto_allowed(monkeypatch):
    # 自建内网 LLM 网关：运营方配置的 endpoint 即便是私网、且白名单收紧，仍放行
    monkeypatch.setattr(settings, "LLM_BASE_URL", "http://10.1.2.3:8000/v1")
    monkeypatch.setattr(settings, "EGRESS_ALLOWLIST", "docs.python.org")
    _check("http://10.1.2.3:8000/v1/chat")
