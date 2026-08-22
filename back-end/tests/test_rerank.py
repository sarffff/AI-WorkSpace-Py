"""重排客户端：批次换算、越界防护、失败退路。

这里最值得测的**不是**"能不能解析响应"，是两个会静默出错的地方：

1. **跨批次的下标换算。** 智谱单次最多 128 条候选，超了要分批。忘了给第二批
   加偏移量的话，返回的顺序会指向错误的分块——而且不报错，只会给出一个乱序的
   "重排结果"，看起来像模型排得不好。
2. **失败必须退回融合序，不能抛。** 重排是增强不是依赖，它挂了整次检索还该出
   结果（BM25 那一路还在）。
"""
from __future__ import annotations

import pytest

from config import settings
from services import rerank
from services.rerank import RerankError, RerankClient
from conftest import run


class _Response:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    """替 httpx.AsyncClient。记下每一次请求体，好断言分批与截断。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers or {}, "json": json or {}})
        if not self._responses:
            raise AssertionError("请求次数超过预置的响应个数")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "RERANK_MODEL", "rerank")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-key")
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://example.invalid/v4/")
    monkeypatch.setattr(settings, "TELEMETRY_ENABLED", False)


def _install(monkeypatch, responses) -> _FakeClient:
    client = _FakeClient(responses)
    monkeypatch.setattr(rerank.httpx, "AsyncClient", lambda **_kw: client)
    return client


def _payload(pairs, prompt_tokens: int = 10) -> _Response:
    """成功响应。必须包成 _Response——被测代码会先调 raise_for_status()。

    之前这里返回裸 dict，8 条用例挂在 `'dict' object has no attribute
    'raise_for_status'`，而失败信息指向 rerank.py 内部，看着像被测代码坏了。
    直接用 _Response 的那几条（500、非 dict、缺字段）一直是通的，因为它们没走
    这个辅助函数。
    """
    return _Response(
        {
            "results": [
                {"index": index, "relevance_score": score} for index, score in pairs
            ],
            "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        }
    )


# ========== 配置 ==========


def test_endpoint_is_built_from_base_url():
    assert RerankClient().endpoint == "https://example.invalid/v4/rerank"


def test_falls_back_to_llm_credentials(monkeypatch):
    """智谱的 rerank 和对话共用凭证，所以留空必须回退而不是报"未配置"。"""
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://llm.invalid/v4/")
    monkeypatch.setattr(settings, "LLM_API_KEY", "llm-key")

    client = RerankClient()

    assert client.endpoint == "https://llm.invalid/v4/rerank"
    assert client.api_key == "llm-key"
    assert client.configured is True


def test_missing_key_is_not_configured(monkeypatch):
    """没配好时调用方应当退回融合序，而不是每次检索都撞一次 401。"""
    monkeypatch.setattr(settings, "RERANK_API_KEY", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")

    assert RerankClient().configured is False


def test_unconfigured_client_raises():
    client = RerankClient()
    with pytest.raises(RerankError):
        run(client.rerank("q", ["a"]))


# ========== 正常路径 ==========


def test_returns_pairs_sorted_by_score(monkeypatch):
    _install(monkeypatch, [_payload([(0, 0.2), (1, 0.9), (2, 0.5)])])

    result = run(RerankClient().rerank("报销标准", ["a", "b", "c"]))

    assert result == [(1, 0.9), (2, 0.5), (0, 0.2)]


def test_empty_documents_skip_the_request(monkeypatch):
    client = _install(monkeypatch, [])

    assert run(RerankClient().rerank("q", [])) == []
    assert client.requests == []


def test_top_n_truncates_after_sorting(monkeypatch):
    _install(monkeypatch, [_payload([(0, 0.1), (1, 0.9), (2, 0.5)])])

    result = run(RerankClient().rerank("q", ["a", "b", "c"], top_n=2))

    assert result == [(1, 0.9), (2, 0.5)]


def test_request_body_shape(monkeypatch):
    client = _install(monkeypatch, [_payload([(0, 1.0)])])

    run(RerankClient().rerank("报销", ["文档"]))

    body = client.requests[0]["json"]
    assert body["model"] == "rerank"
    assert body["query"] == "报销"
    assert body["documents"] == ["文档"]
    # 回显原文会让响应体翻倍，而这里只要下标和分数
    assert body["return_documents"] is False
    assert client.requests[0]["headers"]["Authorization"] == "Bearer test-key"


# ========== 分批与截断 ==========


def test_batches_at_the_documented_limit(monkeypatch):
    documents = [f"doc-{index}" for index in range(rerank.MAX_DOCUMENTS + 30)]
    client = _install(
        monkeypatch, [_payload([(0, 0.5)]), _payload([(0, 0.7)])]
    )

    run(RerankClient().rerank("q", documents))

    assert len(client.requests) == 2
    assert len(client.requests[0]["json"]["documents"]) == rerank.MAX_DOCUMENTS
    assert len(client.requests[1]["json"]["documents"]) == 30


def test_second_batch_indices_are_offset(monkeypatch):
    """忘了加 start 偏移的话排序会指向错误的分块——不报错，只是静默乱序。"""
    documents = [f"doc-{index}" for index in range(rerank.MAX_DOCUMENTS + 5)]
    _install(monkeypatch, [_payload([(0, 0.1)]), _payload([(3, 0.9)])])

    result = run(RerankClient().rerank("q", documents))

    # 第二批的第 3 条 = 全局第 128+3 条
    assert result[0] == (rerank.MAX_DOCUMENTS + 3, 0.9)
    assert result[1] == (0, 0.1)


def test_long_documents_are_clipped(monkeypatch):
    client = _install(monkeypatch, [_payload([(0, 1.0)])])
    long_document = "字" * (rerank.MAX_CHARS + 500)

    run(RerankClient().rerank("q" * (rerank.MAX_CHARS + 500), [long_document]))

    body = client.requests[0]["json"]
    assert len(body["documents"][0]) == rerank.MAX_CHARS
    assert len(body["query"]) == rerank.MAX_CHARS


# ========== 失败与脏响应 ==========


def test_http_failure_becomes_rerank_error(monkeypatch):
    _install(monkeypatch, [_Response({}, status=500)])

    with pytest.raises(RerankError):
        run(RerankClient().rerank("q", ["a"]))


def test_transport_exception_becomes_rerank_error(monkeypatch):
    _install(monkeypatch, [RuntimeError("connection reset")])

    with pytest.raises(RerankError):
        run(RerankClient().rerank("q", ["a"]))


def test_non_dict_response_is_rejected(monkeypatch):
    _install(monkeypatch, [_Response(["not", "a", "dict"])])

    with pytest.raises(RerankError):
        run(RerankClient().rerank("q", ["a"]))


def test_missing_results_is_rejected(monkeypatch):
    _install(monkeypatch, [_Response({"usage": {"prompt_tokens": 1}})])

    with pytest.raises(RerankError):
        run(RerankClient().rerank("q", ["a"]))


def test_out_of_range_indices_are_dropped(monkeypatch):
    """越界下标只可能是提供商侧的 bug。丢一条比让整次检索失败好——
    重排是增强不是依赖。用它当下标会直接 IndexError。"""
    _install(monkeypatch, [_payload([(0, 0.5), (7, 0.9), (-1, 0.8)])])

    result = run(RerankClient().rerank("q", ["a", "b"]))

    assert result == [(0, 0.5)]


def test_malformed_items_are_dropped(monkeypatch):
    _install(
        monkeypatch,
        [
            _Response(
                {
                    "results": [
                        "not a dict",
                        {"index": "0", "relevance_score": 0.9},  # 下标是字符串
                        {"index": True, "relevance_score": 0.9},  # bool 是 int 的子类
                        {"index": 1, "relevance_score": "high"},  # 分数不是数字
                        {"index": 0, "relevance_score": 0.4},
                    ]
                }
            )
        ],
    )

    result = run(RerankClient().rerank("q", ["a", "b"]))

    assert result == [(0, 0.4)]


def test_missing_usage_is_tolerated(monkeypatch):
    """没有 usage 只是算不出成本，不该让重排整个失败。"""
    _install(monkeypatch, [_Response({"results": [{"index": 0, "relevance_score": 1.0}]})])

    assert run(RerankClient().rerank("q", ["a"])) == [(0, 1.0)]
