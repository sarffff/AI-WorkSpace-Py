# -*- coding: utf-8 -*-
"""量一件事：每个结构化输出调用点的 max_tokens 够不够。

起因是记忆抽取那个 bug：``max_tokens`` 写死 512，而 glm-4.5-air 是混合推理模型，
会先花掉一部分预算思考，预算不够时返回的 content 是**空串**——解析不出 JSON，
调用方按"这是增强不是依赖"降级，于是功能 100% 失效而日志干净。

那个 bug 不是孤例，它是一类。全库还有五个同形状的调用点，每个都是
「写死的小 max_tokens + 失败即静默降级 + 丢弃 report」。这个脚本对每个调用点
发两次真实请求：一次用代码里现在的预算，一次用 2048 作对照，然后看

  - content 是不是空串（推理吃光预算的特征）
  - extract_json 能不能抠出合法 JSON
  - 实际输出了多少字符

判据是"同一段提示词在大预算下能解析、在现有预算下不能"——那就不是模型不会做，
是预算给少了。

跑法：cd back-end && python scripts/probe_structured_budgets.py
约 12 次辅助模型调用。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from config import settings  # noqa: E402
from services import prompt_library, structured  # noqa: E402
from services.model_adapter import OpenAICompatibleAdapter  # noqa: E402

CONTROL_BUDGET = 2048

_QUESTION = "跨部门借调期间的绩效由哪一方考核，考核结果怎么合并？"

_SNIPPETS = [
    "借调期间的绩效考核由借入部门负责，考核结果需在考核周期结束后 5 个工作日内同步给借出部门。",
    "员工年度绩效由所在部门主管评定，评定结果计入年度调薪基数。",
    "差旅费报销须在费用发生后 30 个自然日内提交，超期需部门负责人书面说明。",
    "跨部门协作项目的成果归属按项目立项书约定，未约定的由项目管理委员会裁定。",
    "试用期考核不合格的，用人部门应在试用期结束前 10 日内提出书面意见。",
]

# 每一项 = (标签, 源码位置, 现有预算, schema, array, temperature, prompt)
#
# 提示词逐字抄自调用点，不做简化：这里要量的正是"那段提示词在那个预算下能不能跑通"，
# 换一段短提示词就等于换了个问题。
_ROUTE_PROMPT = (
    "判断下面这个检索问题该偏重哪种召回方式，只输出 JSON 对象。\n"
    "- lexical：包含精确的编号、错误码、API 名、专有名词，字面匹配更重要\n"
    "- semantic：改述式提问，措辞和文档大概率不同，语义相似更重要\n"
    "- mixed：两者都有\n"
    '格式：{"intent": "lexical"}，不要任何解释。\n\n'
    f"问题：{_QUESTION}"
)

_HYDE_PROMPT = (
    "为下面这个问题写一段听起来像是从公司内部文档里摘出来的答案，"
    "两三句话，用文档式的书面措辞和术语。不确定的细节可以编，"
    "这段文字只用于检索、不会展示给任何人。\n"
    '只输出 JSON：{"answer": "..."}，不要任何解释。\n\n'
    f"问题：{_QUESTION}"
)

_REWRITE_PROMPT = (
    "把下面的检索问题改写成 2 个不同措辞的检索查询，"
    "覆盖同义词、专业术语和更具体的表述。"
    '只输出 JSON 字符串数组，例如 ["查询1", "查询2"]，不要任何解释。\n\n'
    f"问题：{_QUESTION}"
)

_RERANK_PROMPT = (
    "下面是候选参考片段。请按与问题的相关程度从高到低排序，"
    "完全不相关的片段直接省略。只输出片段编号组成的 JSON 数组，"
    "例如 [3, 1, 5]，不要任何解释。\n\n"
    f"问题：{_QUESTION}\n\n"
    + "\n\n".join(f"[{i}] {s}" for i, s in enumerate(_SNIPPETS, start=1))
)

_EXTRACT_PROMPT = prompt_library.render(
    "memory_extract",
    question="我在财务部，负责差旅报销的合规审核。以后回答请简短一点。报销时限是多久？",
    answer="费用发生后须在 30 个自然日内提交，超期需部门负责人书面说明。",
)

CASES = [
    (
        "query_route",
        "retriever.py:_route_query",
        settings.RAG_ROUTE_MAX_TOKENS,
        structured.QueryRoute,
        False,
        0.0,
        _ROUTE_PROMPT,
    ),
    (
        "hyde",
        "retriever.py:_hyde_query",
        settings.RAG_HYDE_MAX_TOKENS,
        structured.HypotheticalAnswer,
        False,
        0.3,
        _HYDE_PROMPT,
    ),
    (
        "query_rewrite",
        "retriever.py:_expand_queries",
        settings.RAG_MULTI_QUERY_MAX_TOKENS,
        structured.QueryVariants,
        True,
        0.3,
        _REWRITE_PROMPT,
    ),
    (
        "rerank(llm)",
        "retriever.py:_rerank_via_llm",
        settings.RAG_RERANK_MAX_TOKENS,
        structured.RerankOrder,
        True,
        0.0,
        _RERANK_PROMPT,
    ),
    (
        "memory_extract",
        "memory_service.py:extract",
        settings.MEMORY_EXTRACT_MAX_TOKENS,
        structured.MemoryItems,
        True,
        0.0,
        _EXTRACT_PROMPT,
    ),
]


async def probe(adapter, case, budget: int) -> dict:
    label, _where, _current, schema, array, temperature, prompt = case
    # retries=0：这里量的是"一次调用在这个预算下够不够"，重试会把结论搅浑
    # （截断的输出重试一次通常还是截在同一处，见 request_structured 文档串）。
    result, report = await structured.request_structured(
        adapter,
        schema=schema,
        prompt=prompt,
        model=settings.utility_model,
        purpose=f"probe_{label}",
        array=array,
        temperature=temperature,
        max_tokens=budget,
        retries=0,
    )
    raw = report.last_raw or ""
    return {
        "ok": result is not None,
        "raw_len": len(raw),
        "empty": raw.strip() == "",
        "failures": report.failures,
        "preview": raw.strip().replace("\n", " ")[:70],
    }


# ---- 纯文本调用点 -----------------------------------------------------------
#
# 这两处不走 request_structured（要的是自然语言不是 JSON），但失败形状完全一样：
# content 为空串 -> 调用方 `if not raw: return 原值`。而且它们是唯一两个**默认开着**
# 的辅助调用，也就是说真实链路上一直在悄悄退化。
_CONDENSE_TURNS = (
    "user: 跨部门借调的绩效由谁考核？\n"
    "assistant: 借调期间的绩效考核由借入部门负责。\n"
    "user: 那考核结果怎么合并？"
)

PLAIN_CASES = [
    (
        "query_condense",
        "chat_service.py:_condense_query",
        settings.RAG_CONDENSE_MAX_TOKENS,
        0.0,
        prompt_library.render(
            "rag_query_condense",
            recent_turns=_CONDENSE_TURNS,
            question="那考核结果怎么合并？",
        ),
    ),
    (
        "history_summary",
        "conversation_context.py:_summarize",
        settings.HISTORY_SUMMARY_MAX_TOKENS,
        0.2,
        prompt_library.render(
            "history_summary",
            flags={"has_previous": False},
            previous="",
            transcript=(
                "user: 我在财务部，负责差旅报销的合规审核。\n"
                "assistant: 好的。\n"
                "user: 报销时限是多久？\n"
                "assistant: 费用发生后须在 30 个自然日内提交，超期需部门负责人书面说明。\n"
                "user: 超期没写说明会怎样？\n"
                "assistant: 财务有权退单，需重新走审批。"
            ),
        ),
    ),
]


async def probe_plain(adapter, case, budget: int) -> dict:
    _label, _where, _current, temperature, prompt = case
    try:
        completion = await adapter.complete(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            model=settings.utility_model,
            temperature=temperature,
            max_tokens=budget,
            purpose="probe_plain",
        )
    except Exception as exc:  # 通道故障和"预算不够"要分开看
        return {"ok": False, "raw_len": 0, "empty": False, "failures": [type(exc).__name__], "preview": ""}
    raw = (completion.content or "").strip()
    return {
        "ok": bool(raw),
        "raw_len": len(raw),
        "empty": raw == "",
        "failures": [] if raw else ["empty_content"],
        "preview": raw.replace("\n", " ")[:70],
    }


async def main() -> None:
    adapter = OpenAICompatibleAdapter()
    print(f"辅助模型 = {settings.utility_model}    对照预算 = {CONTROL_BUDGET}\n")

    verdicts = []
    for case in CASES:
        label, where, current, *_ = case
        now = await probe(adapter, case, current)
        ctrl = await probe(adapter, case, CONTROL_BUDGET)

        # 三种结论互斥：预算不足 / 两个预算都不行(提示词或模型的问题) / 够用
        if ctrl["ok"] and not now["ok"]:
            verdict = "预算不足"
        elif not ctrl["ok"] and not now["ok"]:
            verdict = "两档都失败"
        else:
            verdict = "够用"
        verdicts.append((label, where, current, verdict))

        print(f"=== {label}  ({where})")
        for name, budget, out in (("现有", current, now), ("对照", CONTROL_BUDGET, ctrl)):
            flag = "解析成功" if out["ok"] else "解析失败"
            empty = "，content 为空串" if out["empty"] else ""
            print(
                f"  {name} max_tokens={budget:<5} {flag}  "
                f"输出 {out['raw_len']} 字{empty}  {out['failures']}"
            )
            if out["preview"]:
                print(f"       > {out['preview']}")
        print(f"  → {verdict}\n")

    await run_plain(adapter, verdicts)

    print("== 汇总 ==")
    for label, where, current, verdict in verdicts:
        mark = "!!" if verdict != "够用" else "  "
        print(f" {mark} {label:<16} max_tokens={current:<5} {verdict:<10} {where}")


async def run_plain(adapter, verdicts: list) -> None:
    print("---- 纯文本调用点（默认开启）----\n")
    for case in PLAIN_CASES:
        label, where, current, *_ = case
        now = await probe_plain(adapter, case, current)
        ctrl = await probe_plain(adapter, case, CONTROL_BUDGET)

        if ctrl["ok"] and not now["ok"]:
            verdict = "预算不足"
        elif not ctrl["ok"] and not now["ok"]:
            verdict = "两档都失败"
        else:
            verdict = "够用"
        verdicts.append((label, where, current, verdict))

        print(f"=== {label}  ({where})")
        for name, budget, out in (("现有", current, now), ("对照", CONTROL_BUDGET, ctrl)):
            flag = "有输出" if out["ok"] else "无输出"
            empty = "，content 为空串" if out["empty"] else ""
            print(
                f"  {name} max_tokens={budget:<5} {flag}  "
                f"输出 {out['raw_len']} 字{empty}  {out['failures']}"
            )
            if out["preview"]:
                print(f"       > {out['preview']}")
        print(f"  → {verdict}\n")


asyncio.run(main())
