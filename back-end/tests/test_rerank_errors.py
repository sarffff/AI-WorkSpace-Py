"""重排 HTTP 失败的分类与日志。

**为什么值得测。** 2026-08-23 之前这里只记 ``type(exc).__name__``，于是 401
（key 属于另一家）、429（没开通额度）、400（模型名不存在）在日志里全是
``HTTPStatusError``——三种完全不同的处理动作长得一模一样。实际后果是智谱
``/rerank`` 返 429 被当成"专用重排没有增益"读了很久，而 rerank-api 变体一次
都没真正执行过。

诊断当时靠的是换模型名对比：``rerank`` 报 429/1113（额度）、``rerank-2`` 报
400/1211（模型不存在）。这个信息本来就在响应体里，只是没被记下来。

同时守住安全约束：**不能把凭证写进日志**。provider 的 error.code 取自响应体
JSON，与 Authorization 头无关；而通用异常分支仍然只记类型，因为那一类的异常
消息可能带上完整请求。
"""
from __future__ import annotations

import httpx
import pytest
from conftest import run

from config import settings
from services.rerank import RerankClient, RerankError


def _client(monkeypatch, **overrides) -> RerankClient:
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "sk-secret-do-not-log")
    monkeypatch.setattr(settings, "RERANK_MODEL", "test-reranker")
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    return RerankClient()


def _stub_response(monkeypatch, status: int, payload: dict | None) -> None:
    """让 httpx 直接返回指定状态码。"""

    async def fake_post(self, url, **kwargs):  # noqa: ANN001
        request = httpx.Request("POST", url)
        return httpx.Response(status, json=payload or {}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


@pytest.mark.parametrize(
    ("status", "code", "hint_fragment"),
    [
        (401, "401", "RERANK_API_KEY"),
        (403, None, "无权访问"),
        (404, None, "RERANK_BASE_URL"),
        (429, "1113", "额度"),
    ],
)
def test_status_code_and_hint_are_logged(
    monkeypatch, caplog, status, code, hint_fragment
):
    client = _client(monkeypatch)
    payload = {"error": {"code": code}} if code else {}
    _stub_response(monkeypatch, status, payload)

    with caplog.at_level("WARNING", logger="rerank"):
        with pytest.raises(RerankError):
            run(client.rerank("q", ["a", "b"]))

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert f"HTTP {status}" in logged
    assert hint_fragment in logged
    if code:
        assert f"provider_code={code}" in logged


def test_model_name_is_logged_so_400_can_be_told_from_429(monkeypatch, caplog):
    """400 要能看出是哪个模型名报的——这是区分"额度"与"模型不存在"的关键。"""
    client = _client(monkeypatch)
    _stub_response(monkeypatch, 400, {"error": {"code": "1211"}})

    with caplog.at_level("WARNING", logger="rerank"):
        with pytest.raises(RerankError):
            run(client.rerank("q", ["a"]))

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "model=test-reranker" in logged
    assert "provider_code=1211" in logged


def test_api_key_never_appears_in_logs(monkeypatch, caplog):
    """凭证不能进日志。错误体可能回显 Authorization，所以只取 error.code。"""
    client = _client(monkeypatch)
    # 故意让响应体里带上凭证，模拟提供商回显请求的情况
    _stub_response(
        monkeypatch,
        401,
        {"error": {"code": "401", "message": "bad key sk-secret-do-not-log"}},
    )

    with caplog.at_level("WARNING", logger="rerank"):
        with pytest.raises(RerankError):
            run(client.rerank("q", ["a"]))

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "sk-secret-do-not-log" not in logged


def test_non_http_failure_logs_only_the_type(monkeypatch, caplog):
    """超时这类异常的消息可能带上完整请求，所以仍然只记类型。"""
    client = _client(monkeypatch)

    async def fake_post(self, url, **kwargs):  # noqa: ANN001
        raise httpx.ConnectTimeout("connecting to https://rerank.example/v1 with sk-secret-do-not-log")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with caplog.at_level("WARNING", logger="rerank"):
        with pytest.raises(RerankError):
            run(client.rerank("q", ["a"]))

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "ConnectTimeout" in logged
    assert "sk-secret-do-not-log" not in logged


def test_unconfigured_client_raises_before_any_request(monkeypatch):
    """没配好时不该发请求——否则每次检索都白撞一次 4xx。"""
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "RERANK_MODEL", "test-reranker")
    client = RerankClient()
    assert client.configured is False
    with pytest.raises(RerankError):
        run(client.rerank("q", ["a"]))
