"""Web 搜索提供商客户端。

知识库回答的是"我存过的资料里怎么说"，回答不了"现在是什么情况"。补上这条通道
之后 Agent 才算有了外部信息源，但它同时把注入面从"我自己下载的文档"扩大到
"任何一个能被搜到的网页"——所以调用方**必须**把结果过 ``guardrails.guard``
再拼进提示词，这一点比知识库那边更要紧。

只做两件事：把不同提供商的响应归一化成 ``SearchResult``，以及在没配置时明确地
说"没配置"。选择哪家由 ``WEB_SEARCH_PROVIDER`` 决定，因为免费额度、地区可用性
和返回质量各家差别很大，写死一家等于把这个决定替使用者做了。

没配置时不抛错也不返回空列表，而是让 ``configured`` 为假、由调用方**根本不注册
这个工具**：注册一个必然失败的工具，模型每轮都会试一次，白烧一轮又拿不到东西。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from config import settings
from services.telemetry import SpanKind, tracer

logger = logging.getLogger("web_search")

# 各提供商的默认端点。允许用 WEB_SEARCH_BASE_URL 覆盖（自建代理、区域端点）
_DEFAULT_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "serper": "https://google.serper.dev/search",
}

PROVIDERS = tuple(_DEFAULT_ENDPOINTS)


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class WebSearchError(RuntimeError):
    """检索通道故障。重试同一个查询没有意义，调用方应尽快收敛。"""


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join((text or "").split())
    if limit <= 0 or len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _parse_tavily(payload: dict, limit: int) -> list[SearchResult]:
    results = []
    for item in (payload.get("results") or [])[: max(0, limit)]:
        if not isinstance(item, dict):
            continue
        results.append(
            SearchResult(
                title=str(item.get("title") or "无标题"),
                url=str(item.get("url") or ""),
                snippet=_clip(
                    str(item.get("content") or ""), settings.WEB_SEARCH_SNIPPET_CHARS
                ),
            )
        )
    return results


def _parse_serper(payload: dict, limit: int) -> list[SearchResult]:
    results = []
    # answerBox 是 Google 直接给出的答案，比 organic 的第一条更贴题，放在最前
    box = payload.get("answerBox")
    if isinstance(box, dict) and (box.get("answer") or box.get("snippet")):
        results.append(
            SearchResult(
                title=str(box.get("title") or "直接答案"),
                url=str(box.get("link") or ""),
                snippet=_clip(
                    str(box.get("answer") or box.get("snippet") or ""),
                    settings.WEB_SEARCH_SNIPPET_CHARS,
                ),
            )
        )
    for item in (payload.get("organic") or []):
        if len(results) >= max(0, limit):
            break
        if not isinstance(item, dict):
            continue
        results.append(
            SearchResult(
                title=str(item.get("title") or "无标题"),
                url=str(item.get("link") or ""),
                snippet=_clip(
                    str(item.get("snippet") or ""), settings.WEB_SEARCH_SNIPPET_CHARS
                ),
            )
        )
    return results[: max(0, limit)]


class WebSearchClient:
    """按配置调用一家提供商。无状态，每次请求新建连接。"""

    @property
    def provider(self) -> str:
        return (settings.WEB_SEARCH_PROVIDER or "").strip().lower()

    @property
    def configured(self) -> bool:
        """未配置时调用方应当**不注册**这个工具，而不是注册一个必然失败的。"""
        return bool(self.provider in _DEFAULT_ENDPOINTS and settings.WEB_SEARCH_API_KEY)

    @property
    def endpoint(self) -> str:
        override = (settings.WEB_SEARCH_BASE_URL or "").strip()
        return override or _DEFAULT_ENDPOINTS[self.provider]

    def _request(self, query: str, limit: int) -> tuple[dict, dict]:
        """返回 (headers, json body)。两家的鉴权位置不同：
        tavily 把 key 放在请求体里，serper 放在头里。"""
        if self.provider == "tavily":
            return (
                {"Content-Type": "application/json"},
                {
                    "api_key": settings.WEB_SEARCH_API_KEY,
                    "query": query,
                    "max_results": limit,
                    "search_depth": "basic",
                },
            )
        return (
            {
                "X-API-KEY": settings.WEB_SEARCH_API_KEY,
                "Content-Type": "application/json",
            },
            {"q": query, "num": limit},
        )

    async def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        if not self.configured:
            raise WebSearchError("web 搜索未配置")

        count = max(1, min(int(limit or settings.WEB_SEARCH_RESULTS), 20))
        headers, body = self._request(query, count)
        async with tracer.span(
            "web_search.query", SpanKind.RETRIEVAL, provider=self.provider, limit=count
        ) as span:
            try:
                async with httpx.AsyncClient(
                    timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS
                ) as client:
                    response = await client.post(
                        self.endpoint, headers=headers, json=body
                    )
                    response.raise_for_status()
                    payload = response.json()
            except Exception as exc:
                # 不记异常消息:某些提供商会把 api_key 回显在错误体里
                span.status = "error"
                span.error_type = type(exc).__name__
                logger.warning(
                    "web search failed via %s: %s", self.provider, type(exc).__name__
                )
                raise WebSearchError("web 搜索请求失败") from exc

            if not isinstance(payload, dict):
                raise WebSearchError("web 搜索返回了无法解析的响应")
            parser = _parse_tavily if self.provider == "tavily" else _parse_serper
            results = parser(payload, count)
            span.set(results=len(results))
            return results


web_search_client = WebSearchClient()

