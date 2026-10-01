"""出站 URL 的出口策略：SSRF 拦截 + host 白名单。

模型可控的出站只有 ``fetch_web_page``（URL 由模型给），它天然是 SSRF / 数据外泄
的入口。这里挡两件事：

1. **SSRF** —— URL（或其重定向目标）指向私网/环回/链路本地/保留段（含云元数据
   ``169.254.169.254``）。这是把"抓网页"变成"探内网 / 读云凭证"的经典手法。
2. **出口白名单** —— ``EGRESS_ALLOWLIST`` 非空时，只放行列出的 host（∪ 运营方自己
   配置的 endpoint host）。空 = 只做 SSRF 拦截、放行任意公网 host，fetch 对公网
   照常可用。

运营方自己配置的 endpoint（LLM / embedding / rerank / web_search / Qdrant 的 host）
永远隐式放行，也不受 SSRF 限制：那是运营方选的、非模型可控，自建内网 LLM 网关
是常态。

已知限制：``check_url`` 解析一次 DNS 判断，httpx 连接时会再解析一次，两次之间存在
DNS rebinding 的 TOCTOU 窗口。本模块挡住直连私网/元数据、以及重定向到私网这些常见
手法；完全防 rebinding 需把解析到的 IP 钉死再连，超出当前范围。
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from config import settings


class EgressBlocked(RuntimeError):
    """出站地址被出口策略拒绝。消息可直接回给模型。"""


def _host_of(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").strip().lower()
    except ValueError:
        return ""


def allowed_hosts() -> set[str]:
    """运营方自己配置的 endpoint host + 显式白名单，永远放行。"""
    hosts: set[str] = set()
    for raw in (
        settings.LLM_BASE_URL,
        settings.EMBEDDING_BASE_URL,
        settings.RERANK_BASE_URL,
        settings.WEB_SEARCH_BASE_URL,
        settings.QDRANT_URL,
    ):
        host = _host_of(raw)
        if host:
            hosts.add(host)
    # web_search 未覆盖 base_url 时用的两个默认端点（见 web_search._DEFAULT_ENDPOINTS）
    hosts.update(("api.tavily.com", "google.serper.dev"))
    for item in (settings.EGRESS_ALLOWLIST or "").split(","):
        item = item.strip().lower()
        if item:
            hosts.add(item)
    return hosts


def _matches(host: str, patterns: set[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("*.") and (
            host == pattern[2:] or host.endswith(pattern[1:])
        ):
            return True
        if host == pattern:
            return True
    return False


def _ip_blocked(ip_text: str) -> bool:
    """字面 IP 是否落在不该出站的段。抽成纯函数便于直接测。"""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


async def check_url(url: str) -> None:
    """放行返回 None；违反出口策略抛 ``EgressBlocked``。"""
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise EgressBlocked(f"只支持 http/https，收到 {scheme or '（无协议）'!r}。")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise EgressBlocked("URL 缺少主机名。")

    # 运营方自配 endpoint / 显式白名单：跳过 SSRF（自建内网网关是常态）与白名单收紧
    is_allowed = _matches(host, allowed_hosts())

    if settings.EGRESS_BLOCK_PRIVATE_IPS and not is_allowed:
        try:
            ips = await _resolve(host)
        except socket.gaierror as exc:
            raise EgressBlocked(f"无法解析主机 {host}：{exc}") from exc
        if any(_ip_blocked(ip) for ip in ips):
            raise EgressBlocked(f"{host} 解析到内网/保留地址，按 SSRF 出口策略拒绝。")

    if (settings.EGRESS_ALLOWLIST or "").strip() and not is_allowed:
        raise EgressBlocked(f"{host} 不在出口白名单内。")
