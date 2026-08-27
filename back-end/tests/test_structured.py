"""结构化输出:Pydantic 约束 + 校验失败重试。

覆盖三件事:契约真的拦得住不合法输出、重试确实把报错回灌给了模型、以及
"抢救排在重试之前"这个顺序——那是省钱的关键,截断的输出重试一次很可能又
截在同一个地方,而抢救不花钱。
"""
from __future__ import annotations

import json

from conftest import ScriptedAdapter, run
from services import structured


class Adapter(ScriptedAdapter):
    """ScriptedAdapter 的 complete 不收 purpose，而 request_structured 会传。"""

    async def complete(
        self, *, messages, tools, model, temperature=0.7, max_tokens=2048, top_p=1.0, purpose="chat"
    ):
        return await super().complete(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
        )


def _ask(adapter, schema, *, array: bool, retries: int = 1, rescue=None):
    return run(
        structured.request_structured(
            adapter,
            schema=schema,
            prompt="p",
            model="m",
            purpose="test",
            array=array,
            retries=retries,
            rescue=rescue,
        )
    )


# ========== extract_json ==========


def test_extract_json_ignores_surrounding_prose():
    assert structured.extract_json('前言 {"a": 1} 后记', array=False) == {"a": 1}
    assert structured.extract_json("```json\n[1, 2]\n```", array=True) == [1, 2]


def test_extract_json_is_greedy_for_nested_structures():
    """非贪婪版本会在第一个内层 ``}`` 就停下,把嵌套对象截成半截。"""
    payload = '{"outer": {"inner": 1}, "tail": 2}'
    assert structured.extract_json(payload, array=False) == {
        "outer": {"inner": 1},
        "tail": 2,
    }


def test_extract_json_respects_requested_shape():
    assert structured.extract_json('{"a": 1}', array=True) is None
    assert structured.extract_json("[1]", array=False) is None


def test_extract_json_handles_empty_and_garbage():
    assert structured.extract_json("", array=False) is None
    assert structured.extract_json("没有 JSON", array=True) is None
    # 形状对但内容不是合法 JSON
    assert structured.extract_json("[not json]", array=True) is None


# ========== 首次即成功 ==========


def test_valid_array_needs_no_retry():
    adapter = Adapter([{"text": '["报销标准", "差旅费用"]'}])

    result, report = _ask(adapter, structured.QueryVariants, array=True)

    assert result is not None
    assert result.items == ["报销标准", "差旅费用"]
    assert report.attempts == 1
    assert not report.retried
    assert report.failures == []


def test_array_payload_is_wrapped_into_items():
    """模型被要求输出裸数组——让它输出 {"items": [...]} 的成功率低得多。"""
    adapter = Adapter([{"text": "[3, 1, 5]"}])

    result, _report = _ask(adapter, structured.RerankOrder, array=True)

    assert result is not None
    assert result.items == [3, 1, 5]


# ========== 校验与重试 ==========


def test_retry_feeds_validation_error_back():
    adapter = Adapter(
        [
            {"text": '[{"kind": "evil", "content": "x"}]'},
            {"text": '[{"kind": "fact", "content": "用户是销售"}]'},
        ]
    )

    result, report = _ask(adapter, structured.MemoryItems, array=True)

    assert result is not None
    assert result.items[0].kind == "fact"
    assert report.attempts == 2
    assert report.failures == ["invalid"]
    # 重试消息里必须带上模型自己那句话,否则它看不到"上一次"指的是什么
    messages = adapter.calls[1]["messages"]
    assert messages[-2]["role"] == "assistant"
    assert "evil" in messages[-2]["content"]
    assert "校验报错" in messages[-1]["content"]
    assert "kind" in messages[-1]["content"]


def test_gives_up_after_retry_budget():
    bad = {"text": '["", "  "]'}
    adapter = Adapter([bad, bad])

    result, report = _ask(adapter, structured.QueryVariants, array=True, retries=1)

    assert result is None
    assert report.attempts == 2
    assert report.failures == ["invalid", "invalid"]
    assert not report.ok


def test_zero_retries_fails_immediately():
    adapter = Adapter([{"text": "根本没有 JSON"}])

    result, report = _ask(adapter, structured.QueryVariants, array=True, retries=0)

    assert result is None
    assert report.attempts == 1
    assert report.failures == ["no_json"]


def test_call_failure_does_not_retry():
    """调用本身失败(超时/限流/鉴权)重试同一段提示词没有意义:错不在格式上。"""
    adapter = Adapter([{"raise": True}, {"text": "[1]"}])

    result, report = _ask(adapter, structured.RerankOrder, array=True, retries=1)

    assert result is None
    assert report.failures == ["call_failed"]
    assert len(adapter.calls) == 1


def test_score_range_is_enforced():
    """裁判给 7 分是坏输出,不该被静默夹到 5——那会把"模型没照做"藏起来。"""
    adapter = Adapter([{"text": '{"success": 7, "grounded": 4}'}])

    result, report = _ask(adapter, structured.TaskScores, array=False, retries=0)

    assert result is None
    assert report.failures == ["invalid"]


# ========== 抢救优先于重试 ==========


def _truncated_scores() -> dict:
    # 分数字段排在前面已经吐出来了,reason 那里被 max_tokens 截断
    return {"text": '{"faithfulness": 5, "relevance": 4, "reason": "依据充'}


def test_rescue_runs_before_retry_and_costs_nothing():
    adapter = Adapter([_truncated_scores()])

    def rescue(raw: str):
        assert "faithfulness" in raw
        return {"faithfulness": 5, "relevance": 4, "reason": "抢救"}

    result, report = _ask(
        adapter, structured.AnswerScores, array=False, retries=1, rescue=rescue
    )

    assert result is not None
    assert result.faithfulness == 5
    assert report.rescued
    assert report.ok
    # 关键:没有第二次调用
    assert len(adapter.calls) == 1


def test_rescue_result_still_goes_through_schema():
    """抢救不走后门:拼出来的 dict 一样要过校验。"""
    adapter = Adapter([{"text": "坏输出"}, {"text": "还是坏输出"}])

    result, report = _ask(
        adapter,
        structured.AnswerScores,
        array=False,
        retries=1,
        rescue=lambda _raw: {"faithfulness": 99, "relevance": 1},
    )

    assert result is None
    assert not report.rescued


def test_retry_happens_when_rescue_declines():
    adapter = Adapter(
        [{"text": "坏输出"}, {"text": '{"faithfulness": 3, "relevance": 3}'}]
    )

    result, report = _ask(
        adapter,
        structured.AnswerScores,
        array=False,
        retries=1,
        rescue=lambda _raw: None,
    )

    assert result is not None
    assert report.attempts == 2
    assert not report.rescued


# ========== 契约细节 ==========


def test_memory_item_rejects_oversized_content(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "MEMORY_ITEM_MAX_CHARS", 10)
    adapter = Adapter([{"text": json.dumps([{"kind": "fact", "content": "x" * 20}])}])

    result, report = _ask(adapter, structured.MemoryItems, array=True, retries=0)

    assert result is None
    assert report.failures == ["invalid"]


def test_query_variants_strips_and_rejects_all_blank():
    adapter = Adapter([{"text": '["  报销  ", ""]'}])

    result, _report = _ask(adapter, structured.QueryVariants, array=True, retries=0)

    assert result is not None
    assert result.items == ["报销"]


def test_rerank_rejects_zero_and_negative():
    adapter = Adapter([{"text": "[0, 1]"}])

    result, report = _ask(adapter, structured.RerankOrder, array=True, retries=0)

    assert result is None
    assert report.failures == ["invalid"]


def test_abstention_requires_boolean():
    # 注意用 "maybe" 而不是 "yes":Pydantic 的宽松模式会把 "yes"/"true"/"1"
    # 这类字符串强转成 bool,拿它们当反例测不出契约有没有生效。
    adapter = Adapter([{"text": '{"abstained": "maybe"}'}])

    result, report = _ask(
        adapter, structured.AbstentionVerdict, array=False, retries=0
    )

    assert result is None
    assert report.failures == ["invalid"]


# ========== 预算耗尽 ==========
#
# 这一组钉住的是这个仓库里代价最大的一次故障:混合推理模型先花 max_tokens 思考,
# 预算不够时返回**空串**(不是截断的 JSON)。空串解析不出 JSON,而每个调用方都
# 合理地"这是增强不是依赖"静默降级。全库 7 个辅助调用点有 5 个长期落在这里,
# 其中 4 个恰好是 eval 的变体——报告上读作"这个技术没有增益"。
#
# 判据是 finish_reason=length **且正文为空**。两个条件都必要:
# 只看 finish_reason 会把"答长了被截断"也算进来(那种还有内容可以 rescue);
# 只看空串会把"模型主动啥也不说"算进来(那不是预算问题)。


def _exhausted() -> dict:
    return {"text": "", "finish_reason": "length"}


def test_budget_exhaustion_gets_its_own_failure_label():
    """``truncated`` 与 ``no_json`` 一起出现——两者的修法完全不同。

    ``no_json`` 会让人去改提示词,而这里该改的是 max_tokens。混在一个标签里
    等于没有信息。
    """
    adapter = Adapter([_exhausted()])

    result, report = _ask(adapter, structured.QueryVariants, array=True, retries=0)

    assert result is None
    assert report.failures == ["no_json", "truncated"]
    assert report.budget_exhausted
    assert report.finish_reason == "length"


def test_budget_exhaustion_skips_retry():
    """和 call_failed 同理:重试解决不了预算问题,只会白花一次调用。

    而且更糟——回灌的 assistant 空消息加纠正说明会占掉输入,下一次思考的
    余地只会更小。
    """
    adapter = Adapter([_exhausted(), {"text": '["报销标准"]'}])

    result, report = _ask(adapter, structured.QueryVariants, array=True, retries=1)

    assert result is None
    assert report.attempts == 1
    assert len(adapter.calls) == 1, "预算耗尽时不该重试"


def test_truncated_but_non_empty_still_retries():
    """答长了被截断是另一回事:内容还在,重试(或 rescue)有意义。

    分不清这两种就会走错路——把"输出太长"当成预算不足去加 max_tokens 是浪费,
    把"预算被思考吃光"当成格式问题去改提示词是白改。
    """
    adapter = Adapter(
        [
            {"text": '["报销标', "finish_reason": "length"},
            {"text": '["报销标准"]'},
        ]
    )

    result, report = _ask(adapter, structured.QueryVariants, array=True, retries=1)

    assert result is not None
    assert report.attempts == 2
    assert not report.budget_exhausted
    assert "truncated" not in report.failures


def test_budget_exhaustion_is_logged(caplog):
    """必须留日志:静默降级是这类故障能藏这么久的唯一原因。"""
    import logging

    adapter = Adapter([_exhausted()])

    with caplog.at_level(logging.WARNING, logger="structured"):
        _ask(adapter, structured.QueryVariants, array=True, retries=0)

    assert any("exhausted" in record.message for record in caplog.records)


def test_default_max_tokens_is_not_on_the_failing_side():
    """默认值只对**下一个**调用方生效,所以它必须站在安全的一侧。

    原来是 512,而 512 实测正好落在"返回空串"那一侧
    (见 scripts/probe_structured_budgets.py)。
    """
    import inspect

    signature = inspect.signature(structured.request_structured)
    assert signature.parameters["max_tokens"].default >= 1024
