"""显式规划（plan-and-execute）的规划那一半。

覆盖三类风险，按重要性排：

1. **静默失效。** 规划是增强不是依赖，失败就退回纯 ReAct——这个取舍是对的，
   但它意味着"规划挂了"和"模型判断不用分步"在返回值上完全同形（都是空计划）。
   本仓库同一形状的故障发生过五次（七个辅助调用点有五个因为 max_tokens 给小了
   100% 返回空串，而每个调用方都合理地静默降级），所以这里逐条钉住：
   失败必留日志、预算必须来自配置且按思考开销给。
2. **契约。** 步数上限是校验而不是截断，超长 goal 触发重试而不是被接受。
3. **编出来的工具名。** 只清掉那一步的 tool，不废掉整份计划。
"""
from __future__ import annotations

import json
import logging

import pytest

from conftest import ScriptedAdapter, run
from services import planner, structured


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


@pytest.fixture(autouse=True)
def _plan_mode_on(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "AGENT_PLAN_MODE", "plan_execute")


def _plan(steps: list[dict]) -> dict:
    return {"text": json.dumps(steps, ensure_ascii=False)}


def _build(adapter, *, tools=("search_knowledge_base", "calculate")):
    return run(
        planner.build_plan(
            adapter,
            question="报销超期了怎么办，最多能报多少",
            context="（无）",
            tool_names=list(tools),
        )
    )


# ========== 关掉时是空操作 ==========


def test_disabled_makes_no_call(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "AGENT_PLAN_MODE", "off")
    adapter = Adapter([])

    assert _build(adapter) == []
    assert adapter.calls == [], "关掉规划时不该有任何模型调用"


# ========== 正常产出 ==========


def test_builds_steps_with_tools():
    adapter = Adapter(
        [
            _plan(
                [
                    {"goal": "查报销超期的处理规定", "tool": "search_knowledge_base"},
                    {"goal": "算出可报金额", "tool": "calculate"},
                ]
            )
        ]
    )

    steps = _build(adapter)

    assert [step["goal"] for step in steps] == ["查报销超期的处理规定", "算出可报金额"]
    assert [step["tool"] for step in steps] == ["search_knowledge_base", "calculate"]


def test_empty_plan_is_a_valid_answer():
    """空计划是正确输出，不是失败。

    plan-and-execute 出名的失效模式是给简单问题硬凑步骤，所以提示词明写
    "不需要工具就输出 []"。契约必须接受它，否则那条指令等于没写。
    """
    adapter = Adapter([_plan([])])

    assert _build(adapter) == []


def test_pure_reasoning_step_keeps_empty_tool():
    """比较、汇总、下结论这类步骤没有对应工具。

    硬要求每步都填一个工具就是直接鼓励模型乱调。
    """
    adapter = Adapter([_plan([{"goal": "对比两处规定的差异", "tool": ""}])])

    steps = _build(adapter)

    assert steps == [{"goal": "对比两处规定的差异", "tool": ""}]


# ========== 静默失效的防线 ==========


def test_uses_configured_token_budget():
    """预算必须来自配置。

    钉住的是本仓库最贵的一类故障：写死一个"够输出几行 JSON"的小数字，而推理型
    模型先花预算思考，不够时返回空串 → 解析不出 JSON → 静默退回纯 ReAct。
    七个辅助调用点里有五个曾经栽在这上面。
    """
    from config import settings

    adapter = Adapter([_plan([{"goal": "查规定", "tool": "search_knowledge_base"}])])
    _build(adapter)

    assert adapter.calls[0]["max_tokens"] == settings.AGENT_PLAN_MAX_TOKENS
    # 实测 512 落在"返回空串"那一侧，默认值必须留余量
    assert settings.AGENT_PLAN_MAX_TOKENS >= 1024


def test_failure_falls_back_and_logs(caplog):
    """解析不出计划时退回纯 ReAct，但必须留日志。

    少了这条日志，"规划挂了"和"模型判断不用分步"在外部完全同形——两者都是
    空计划，而前者是需要立刻修的故障。
    """
    adapter = Adapter([{"text": ""}, {"text": ""}])

    with caplog.at_level(logging.WARNING, logger="planner"):
        steps = _build(adapter)

    assert steps == []
    assert any("no usable plan" in record.message for record in caplog.records)


def test_budget_exhaustion_is_visible_as_such(caplog):
    """预算被思考吃光时，日志里要能看出是截断而不是格式问题。

    两者的修法完全不同：一个改 max_tokens，一个改提示词。
    """
    adapter = Adapter([{"text": "", "finish_reason": "length"}])

    with caplog.at_level(logging.WARNING, logger="planner"):
        _build(adapter)

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "length" in messages


# ========== 契约 ==========


def test_step_limit_is_validated_not_truncated(monkeypatch):
    """超出 max_steps 触发重试，而不是砍掉多余的步骤。

    截断等于把"模型没照 max_steps 做"翻译成"计划就这么长"，而后者不会有人去查。
    """
    from config import settings

    monkeypatch.setattr(settings, "AGENT_PLAN_MAX_STEPS", 2)
    monkeypatch.setattr(settings, "STRUCTURED_OUTPUT_RETRIES", 1)
    too_many = _plan([{"goal": f"第 {i} 步", "tool": ""} for i in range(5)])
    adapter = Adapter([too_many, _plan([{"goal": "并成一步", "tool": ""}])])

    steps = _build(adapter)

    assert [step["goal"] for step in steps] == ["并成一步"]
    assert len(adapter.calls) == 2, "超限应当触发一次重试"
    assert "最多 2 步" in adapter.calls[1]["messages"][-1]["content"]


def test_oversized_goal_is_rejected(monkeypatch):
    """一个"步骤"写到两百字就不是步骤了，是把整段回答提前写完。"""
    from config import settings

    monkeypatch.setattr(settings, "STRUCTURED_OUTPUT_RETRIES", 0)
    adapter = Adapter([_plan([{"goal": "x" * 250, "tool": ""}])])

    assert _build(adapter) == []


def test_unknown_tool_is_dropped_but_plan_survives(caplog):
    """编出来的工具名只清掉那一步的 tool，不废掉整份计划。

    计划的价值在 goal 上——为一个错的工具名扔掉整份计划不值得。
    工具名的合法性也不该进契约：可用工具随开关和委派模式变，那是调用方的信息。
    """
    adapter = Adapter(
        [
            _plan(
                [
                    {"goal": "查规定", "tool": "search_knowledge_base"},
                    {"goal": "发邮件通知", "tool": "send_email"},
                ]
            )
        ]
    )

    with caplog.at_level(logging.WARNING, logger="planner"):
        steps = _build(adapter)

    assert len(steps) == 2
    assert steps[1] == {"goal": "发邮件通知", "tool": ""}
    assert any("unknown tool" in record.message for record in caplog.records)


# ========== 注入用的渲染 ==========


def test_format_steps_marks_toolless_steps():
    text = planner.format_steps(
        [
            {"goal": "查规定", "tool": "search_knowledge_base"},
            {"goal": "下结论", "tool": ""},
        ]
    )

    assert "1. 查规定（用 search_knowledge_base）" in text
    assert "2. 下结论（不需要工具）" in text
    # 措辞必须留出"计划可以改"的空间：把它写成命令会让模型宁可按错计划走完，
    # 也不肯在检索空了的时候换个查询——那正好废掉 ReAct 的长处
    assert "不是必须逐条执行的命令" in text


def test_plan_contract_accepts_missing_tool_field():
    """``tool`` 缺省即空串：模型省略它比填一个空字符串更常见。"""
    value = structured.Plan.model_validate({"items": [{"goal": "只写目标"}]})

    assert value.items[0].tool == ""
