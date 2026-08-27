"""重排（rerank）客户端。

**为什么要一个专门的 cross-encoder，而不是继续让通用模型排序。**

现在的 ``retriever._maybe_rerank`` 是 LLM listwise：把候选片段编号列给模型，让它
输出一个顺序。它能用，但学到的是提示词工程，不是检索工程，而且有三个结构性问题：

1. 排序质量取决于模型愿不愿意认真读那 20 段文字，而它没有任何理由认真读；
2. 输出是一串编号，没有分数——拿不到"这条到底多相关"，没法设阈值筛掉尾部；
3. 候选一多提示词就撑爆，而 listwise 的注意力本来就偏向开头几条。

cross-encoder 是另一回事：query 和 document **拼在一起**过一遍模型，输出一个标量
相关度。它比稠密检索准的原因也在这里——稠密检索是 bi-encoder，query 和 document
各自独立编码成向量，两者在编码时**从未见过对方**，所以"这段文字是否回答了这个
问题"这种交互信息压根没进向量。代价是没法预先索引：每个 (query, doc) 对都要算
一次，所以它只能放在召回之后当精排。

智谱有现成的 rerank 接口，用的是同一个 API key、同一个 base URL，所以这里不需要
torch、不需要本地模型、不需要新的凭证。约束来自它的文档：单次最多 128 条候选，
单条与 query 各最长 4096 字符。

结构照 ``services/web_search.py``：客户端对象 + ``configured`` 属性 + 埋点 +
专用异常 + 不记异常消息只记类型（错误体可能回显 key）。
"""
from __future__ import annotations

import logging

import httpx

from config import settings
from services.telemetry import SpanKind, TokenSource, tracer

logger = logging.getLogger("rerank")

# 智谱文档写死的上限。超了不是降级而是直接 400，所以必须在客户端侧分批与截断。
MAX_DOCUMENTS = 128
MAX_CHARS = 4096


class RerankError(RuntimeError):
    """重排通道故障。调用方应当退回融合序，而不是让整次检索失败。

    ``code`` 是给**机器**看的简短分类（``http_429`` / ``timeout`` /
    ``unconfigured``…），消息是给人看的。分开是因为 eval 要把降级原因汇总进报告，
    而按消息文本分组等于拿人类可读的句子当枚举用——改一个字就把历史数据割开了。

    不要把 provider 返回的原始消息塞进 ``code``：那一类消息里可能带上完整请求
    （含 Authorization 头），而 ``code`` 会进报告和埋点。
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _clip(text: str) -> str:
    collapsed = (text or "").strip()
    return collapsed[:MAX_CHARS]


class RerankClient:
    """按配置调用重排接口。无状态，每次请求新建连接。"""

    @property
    def endpoint(self) -> str:
        base = (settings.RERANK_BASE_URL or settings.LLM_BASE_URL or "").rstrip("/")
        return f"{base}/rerank"

    @property
    def api_key(self) -> str:
        return settings.RERANK_API_KEY or settings.LLM_API_KEY

    @property
    def configured(self) -> bool:
        """没配好时调用方应当退回 LLM 重排或不重排，而不是每次检索都撞一次 400。"""
        return bool(self.api_key and settings.RERANK_MODEL and self.endpoint)

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """返回 ``[(原始下标, 相关度), ...]``，按相关度降序。

        下标是**相对于传进来的 documents 列表**的，跨批次已经换算好——调用方
        不需要知道这里分了几批。
        """
        if not self.configured:
            raise RerankError("重排接口未配置")
        if not documents:
            return []

        clipped_query = _clip(query)
        async with tracer.span(
            "rerank.score",
            SpanKind.RETRIEVAL,
            model=settings.RERANK_MODEL,
            documents=len(documents),
            batches=(len(documents) + MAX_DOCUMENTS - 1) // MAX_DOCUMENTS,
        ) as span:
            scored: list[tuple[int, float]] = []
            prompt_tokens = 0
            for start in range(0, len(documents), MAX_DOCUMENTS):
                batch = documents[start : start + MAX_DOCUMENTS]
                payload, usage = await self._request(clipped_query, batch)
                # 批内下标换算回全局下标。忘了 start 的话第二批之后的排序
                # 全部指向错误的分块——而且不会报错，只会静默给出乱序。
                scored.extend((start + index, score) for index, score in payload)
                prompt_tokens += usage

            if prompt_tokens:
                # 重排只有输入侧成本，但它和主模型不同价：价目表里要单独一项，
                # 否则这部分开销会按主模型的价算，成本列偏高
                span.set_usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=0,
                    source=TokenSource.PROVIDER,
                    model=settings.RERANK_MODEL,
                )
            scored.sort(key=lambda item: (-item[1], item[0]))
            if top_n is not None and top_n > 0:
                scored = scored[:top_n]
            span.set(returned=len(scored), top_score=scored[0][1] if scored else None)
            return scored

    async def _request(
        self, query: str, documents: list[str]
    ) -> tuple[list[tuple[int, float]], int]:
        body = {
            "model": settings.RERANK_MODEL,
            "query": query,
            "documents": [_clip(document) for document in documents],
            # return_documents 默认就是 false，显式写上：回显原文会让响应体
            # 翻倍，而这里只需要下标和分数
            "return_documents": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=settings.RERANK_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(self.endpoint, headers=headers, json=body)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            # 状态码必须记：401/429/400 指向三种完全不同的处理动作（换 key /
            # 开额度 / 改模型名），而只记异常类型的话它们在日志里长得一模一样。
            #
            # 2026-08-23 就是这么踩的：智谱 /rerank 返 429，被当成"重排没有增益"
            # 读了很久。诊断靠的是换模型名对比——429/1113 是额度、400/1211 是
            # 模型不存在。所以这里也把 provider 的 code 带出来（它在响应体里，
            # 与 Authorization 头无关，不会回显凭证）。
            status = exc.response.status_code
            code = None
            try:
                payload = exc.response.json()
                if isinstance(payload, dict):
                    error = payload.get("error")
                    if isinstance(error, dict):
                        code = error.get("code")
            except Exception:
                pass
            hint = {
                401: "凭证无效——检查 RERANK_API_KEY 是否属于 RERANK_BASE_URL 那一家",
                403: "凭证无权访问该模型",
                404: "端点不存在——检查 RERANK_BASE_URL 是否需要 /v1 前缀",
                429: "额度或频率受限——账号可能没开通该项服务",
            }.get(status, "")
            logger.warning(
                "rerank request failed: HTTP %s provider_code=%s model=%s %s",
                status, code, settings.RERANK_MODEL, hint,
            )
            # code 里带上 provider code：429/1113(额度没开通)和 429/普通限流
            # 的处理动作不同,前者换端点、后者重试
            raise RerankError(
                f"重排请求失败 (HTTP {status})",
                code=f"http_{status}" + (f"_{code}" if code else ""),
            ) from exc
        except Exception as exc:
            # 超时、连接失败、JSON 解析失败等。不记异常消息：这一类的消息里
            # 可能带上完整请求（含 Authorization 头）。
            logger.warning("rerank request failed: %s", type(exc).__name__)
            raise RerankError("重排请求失败", code=type(exc).__name__) from exc

        if not isinstance(data, dict):
            raise RerankError("重排返回了无法解析的响应")
        return _parse_results(data, len(documents)), _usage_tokens(data)


def _parse_results(payload: dict, batch_size: int) -> list[tuple[int, float]]:
    """从响应里取出 (下标, 分数)。越界与非法项丢掉而不是抛错。

    越界下标只可能是提供商侧的 bug，丢掉一条比让整次检索失败好——重排本来就是
    增强，不是依赖。
    """
    results = payload.get("results")
    if not isinstance(results, list):
        raise RerankError("重排响应里没有 results")

    parsed: list[tuple[int, float]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        score = item.get("relevance_score")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not 0 <= index < batch_size:
            continue
        try:
            parsed.append((index, float(score)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return parsed


def _usage_tokens(payload: dict) -> int:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0
    value = usage.get("prompt_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


rerank_client = RerankClient()
