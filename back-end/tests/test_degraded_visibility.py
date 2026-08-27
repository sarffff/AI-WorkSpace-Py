"""降级可见性：配了但没生效，必须在报告里读得出来。

这个文件测的不是"降级路径对不对"——那部分一直是对的，增强失败退回融合序是
应该的。测的是**降级有没有被记下来**。

背景值得写清楚，因为它是这套 eval 最贵的一次错误：

- 2026-08-22 查出 5 个辅助模型调用点 100% 返回空串（推理模型把 max_tokens
  花在思考上），对应的 eval 变体与 baseline **逐位相同**，被读成"这个技术
  没有增益"，持续了很久。
- 2026-08-23 又查出 ``rerank-api`` 从未执行过一次：智谱 ``/rerank`` 对未开通
  该项额度的账号返 429/1113，``_rerank_via_api`` 静默退回融合序。报告里同样
  与 baseline 逐位相同。

两次的形状一模一样：**"没跑"和"没用"在报告里长得一样，而处置完全相反**——
前者去修配置，后者去掉这个技术。所以降级必须冒泡到结论所在的那一层。

只写 logger 是不够的：读报告的人不看日志。
"""
from __future__ import annotations

import pytest

from config import settings
from services import rerank, retriever, telemetry
from services.rerank import RerankError
from services.telemetry import SpanKind, tracer
from conftest import run


@pytest.fixture(autouse=True)
def _telemetry_on(monkeypatch):
    """降级要写到 span 上，所以埋点必须开——关掉时 current_span() 是 NoopSpan。

    同时挡住落库：``_flush`` 自己会吞掉所有异常（埋点不能影响业务），所以不挡
    也能过，但那样测试就依赖了"数据库连不上"这个副作用，换个环境就变慢或变绿
    得莫名其妙。
    """
    monkeypatch.setattr(settings, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_MODEL", "test-reranker")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-key")
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://example.invalid/v1/")
    monkeypatch.setattr(telemetry, "_persist", lambda _trace: None)


async def _capture(fn) -> list[dict]:
    """在一条 trace 里跑 fn，返回所有 span 的 attributes。"""
    async with tracer.trace() as trace:
        async with tracer.span("outer", SpanKind.RETRIEVAL):
            await fn()
        return [dict(span.attributes) for span in trace.spans]


# ========== _mark_degraded 本身 ==========


def test_mark_degraded_writes_stage_and_fallback_onto_current_span():
    async def body():
        retriever._mark_degraded("rerank_api", "请求失败，退回融合序")

    attrs = run(_capture(body))
    marked = [a for a in attrs if a.get("degraded_stage")]
    assert len(marked) == 1
    assert marked[0]["degraded_stage"] == "rerank_api"
    assert marked[0]["degraded_fallback"] == "请求失败，退回融合序"


def test_mark_degraded_is_harmless_without_an_active_span():
    """生产链路上重排也可能在 trace 之外被调用，不能因此炸掉整次检索。"""
    retriever._mark_degraded("rerank_api", "请求失败")  # 不抛就算过


# ========== 429：一次都没生效的那个 ==========


def test_rerank_api_request_failure_is_marked(monkeypatch):
    """智谱 /rerank 对无额度账号返 429。退回融合序是对的，但必须留痕。"""

    async def boom(*_a, **_kw):
        raise RerankError("重排请求失败")

    monkeypatch.setattr(rerank.rerank_client, "rerank", boom)

    async def body():
        out = await retriever.HybridRetriever()._rerank_via_api(
            "q", ["c1", "c2"], ["s1", "s2"]
        )
        # 退回融合序：返回空列表，让调用方保持原顺序
        assert out == []

    attrs = run(_capture(body))
    stages = [a["degraded_stage"] for a in attrs if a.get("degraded_stage")]
    assert stages == ["rerank_api"]


def test_rerank_api_unconfigured_is_marked(monkeypatch):
    """未配置端点时不静默退回 llm——否则报告里的 mode=api 与实际跑的东西不一致。

    2026-08-27 补：原来只清 3 个设置，而 ``configured`` 仍为 True（``endpoint``
    空 base 时是 ``"/rerank"``、``api_key`` 回落 ``LLM_API_KEY``），于是这条测试
    走的是**请求失败**分支——``stages == ["rerank_api"]`` 在那条路上同样成立，
    所以它绿着却没测到未配置。现在清全 5 个，并顺带断言原因。
    """
    monkeypatch.setattr(settings, "RERANK_API_KEY", "")
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "RERANK_MODEL", "")

    async def body():
        out = await retriever.HybridRetriever()._rerank_via_api(
            "q", ["c1", "c2"], ["s1", "s2"]
        )
        assert out == []

    attrs = run(_capture(body))
    stages = [a["degraded_stage"] for a in attrs if a.get("degraded_stage")]
    assert stages == ["rerank_api"]
    reasons = [a["degraded_reason"] for a in attrs if a.get("degraded_reason")]
    assert reasons == ["unconfigured"], "这才说明走的是未配置分支，不是请求失败分支"


def test_eval_collects_degraded_stages_from_the_whole_trace():
    """降级发生在嵌套的 structured span 里，而 eval 扫的是扁平 span 列表。

    这一条锁住"嵌套多深都捞得到"：真实链路上重排的降级标记写在
    ``structured.rerank`` 这一层，不是最外层的 ``retrieval.hybrid``。
    """
    from eval.runner import _degraded_stages

    async def body():
        async with tracer.span("structured.rerank", SpanKind.LLM):
            retriever._mark_degraded("rerank", "退回融合序")

    class _Trace:
        def __init__(self, spans):
            self.spans = spans

    collected: list = []

    async def outer():
        async with tracer.trace() as trace:
            async with tracer.span("retrieval.hybrid", SpanKind.RETRIEVAL):
                await body()
            collected.extend(trace.spans)

    run(outer())
    assert _degraded_stages(_Trace(collected)) == ["rerank"]


def test_eval_counts_repeated_degradation_rather_than_deduplicating():
    """同一阶段降级多次时次数本身是信息：偶发失败和 100% 失效是两回事。

    LLM 重排在真实语料上是**偶发**降级（12288 预算下 2/30），如果这里去重成
    集合，"偶发"和"每次都挂"就再也分不开了。
    """
    from eval.runner import _degraded_stages

    class _Span:
        def __init__(self, attrs):
            self.attributes = attrs

    trace = type("T", (), {"spans": [
        _Span({"degraded_stage": "rerank"}),
        _Span({"degraded_stage": "rerank"}),
        _Span({}),
        _Span({"degraded_stage": "hyde"}),
    ]})()
    assert _degraded_stages(trace) == ["rerank", "rerank", "hyde"]


def test_degraded_stages_is_empty_without_a_trace():
    """埋点关掉时 trace 是 None，不能因此炸掉整次 eval。"""
    from eval.runner import _degraded_stages

    assert _degraded_stages(None) == []


def test_successful_rerank_is_not_marked(monkeypatch):
    """反向断言：成功时不能留降级痕迹，否则计数器永远非零，等于没有信号。"""

    async def ok(_query, _snippets, top_n=None):
        return [(1, 0.9), (0, 0.1)]

    monkeypatch.setattr(rerank.rerank_client, "rerank", ok)

    async def body():
        out = await retriever.HybridRetriever()._rerank_via_api(
            "q", ["c1", "c2"], ["s1", "s2"]
        )
        assert out == ["c2", "c1"]

    attrs = run(_capture(body))
    assert not [a for a in attrs if a.get("degraded_stage")]


# ========== 降级**原因**也要留痕 ==========
#
# 次数回答"生效了没有"，原因回答"该改哪里"。只有次数时下一步只能靠猜——
# 2026-08-27 那份报告说 rerank 降级 10 次，而 truncated（加预算）、no_json
# （换提示词）、http_429（换端点）的修法完全不同。


def test_http_failure_carries_the_status_code_as_a_reason(monkeypatch):
    """HTTP 失败的原因要带状态码：401/429/400 指向三种不同的处理动作。"""

    async def boom(*_a, **_kw):
        raise RerankError("重排请求失败 (HTTP 429)", code="http_429_1113")

    monkeypatch.setattr(rerank.rerank_client, "rerank", boom)

    async def body():
        await retriever.HybridRetriever()._rerank_via_api("q", ["c1"], ["s1"])

    attrs = run(_capture(body))
    reasons = [a["degraded_reason"] for a in attrs if a.get("degraded_reason")]
    assert reasons == ["http_429_1113"], (
        "429/1113 是额度没开通、要换端点；普通 429 是限流、该重试。只记"
        "「请求失败」这两件事分不开。"
    )


def test_unconfigured_endpoint_has_its_own_reason(monkeypatch):
    """没配端点和配了但失败是两件事，原因要分得开。

    ## 为什么要清 4 个设置而不是 3 个

    ``RerankClient.configured`` 是 ``api_key and RERANK_MODEL and endpoint``，而：

    - ``endpoint`` 返回 ``f"{base}/rerank"``，base 空时是 ``"/rerank"`` —— **真值**
    - ``api_key`` 会回落到 ``LLM_API_KEY``，而 ``.env`` 里有

    所以只清 RERANK_* 和 LLM_BASE_URL 时 ``configured`` 仍然为 True，会真的发一次
    请求并走**失败**分支。上面那条 ``test_rerank_api_unconfigured_is_marked`` 就是
    这样：它断言的 ``stages == ["rerank_api"]`` 在失败分支上同样成立，所以它一直
    绿着，却从没真正测到未配置那条路。是这条测试断言了原因才把它暴露出来。
    """
    monkeypatch.setattr(settings, "RERANK_API_KEY", "")
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "RERANK_MODEL", "")

    async def body():
        await retriever.HybridRetriever()._rerank_via_api("q", ["c1"], ["s1"])

    attrs = run(_capture(body))
    reasons = [a["degraded_reason"] for a in attrs if a.get("degraded_reason")]
    assert reasons == ["unconfigured"]


def test_reason_is_absent_rather_than_unknown_when_there_is_none():
    """没有原因时不写这个键，而不是写 "unknown"。

    缺失和"确实不知道"在汇总时该分得开——写死 unknown 会让"埋点没接上"
    和"这次真的拿不到原因"长得一样。
    """

    async def body():
        retriever._mark_degraded("some_stage", "退回默认")

    attrs = run(_capture(body))
    marked = [a for a in attrs if a.get("degraded_stage")]
    assert marked, "阶段本身要写上"
    assert "degraded_reason" not in marked[0]


def test_structured_failure_reason_reaches_the_span():
    """``_warn_degraded`` 手里有 failures，那就必须传下去。

    改动之前它只写日志：报告能说出"rerank 降级 10 次"却说不出为什么，
    而读报告的人不看日志。
    """

    class _Report:
        attempts = 2
        failures = ["truncated", "no_json"]
        finish_reason = "length"
        budget_exhausted = False

    async def body():
        retriever._warn_degraded("rerank", _Report(), "退回融合序")

    attrs = run(_capture(body))
    marked = [a for a in attrs if a.get("degraded_stage")]
    assert marked[0]["degraded_stage"] == "rerank"
    # 取**最后**一次失败：前面几次可能是重试路上的不同原因
    assert marked[0]["degraded_reason"] == "no_json"


def test_finish_reason_is_used_when_there_are_no_failures():
    """一次都没进 failures 但仍然返回 None 时，退回用 finish_reason。"""

    class _Report:
        attempts = 1
        failures: list[str] = []
        finish_reason = "length"
        budget_exhausted = True

    async def body():
        retriever._warn_degraded("hyde", _Report(), "用原查询")

    attrs = run(_capture(body))
    marked = [a for a in attrs if a.get("degraded_stage")]
    assert marked[0]["degraded_reason"] == "finish_length"


def test_eval_aggregates_reasons_per_stage():
    """eval 把原因按 ``阶段:原因`` 汇总，不和阶段计数混在一起。

    混进 ``degraded_stages`` 会让"同一阶段两种原因"变成两个阶段，
    而那一列是按阶段计数用的。
    """
    from eval.runner import _degraded_reasons, _degraded_stages

    class _Span:
        def __init__(self, **attrs):
            self.attributes = attrs

    class _Trace:
        spans = [
            _Span(degraded_stage="rerank", degraded_reason="truncated"),
            _Span(degraded_stage="rerank", degraded_reason="no_json"),
            _Span(degraded_stage="rerank", degraded_reason="truncated"),
            _Span(other="x"),
        ]

    trace = _Trace()
    # 阶段那一列仍然只数阶段
    assert _degraded_stages(trace) == ["rerank", "rerank", "rerank"]
    assert _degraded_reasons(trace) == [
        "rerank:truncated",
        "rerank:no_json",
        "rerank:truncated",
    ]


def test_degradation_without_a_reason_counts_as_unknown_in_the_summary():
    """没有原因的降级也要计数，归到 unknown。

    少算一次会让 degradedCases 和原因数对不上，而对不上比缺信息更难查。
    """
    from eval.runner import _degraded_reasons

    class _Span:
        def __init__(self, **attrs):
            self.attributes = attrs

    class _Trace:
        spans = [_Span(degraded_stage="rerank_api")]

    assert _degraded_reasons(_Trace()) == ["rerank_api:unknown"]
