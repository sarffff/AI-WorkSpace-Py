# -*- coding: utf-8 -*-
"""按**最坏情况输入**扫辅助调用的输出预算。

为什么需要这一步：``probe_structured_budgets.py`` 用的是玩具输入（一个短问题、
5 个候选），它给出的"够用"对**输入随配置增长**的调用点是假绿灯。已经踩到一次：
重排在 5 个候选下 1024 通过，换成生产配置的 20 个候选就 100% 失败。

思考开销跟着输入长度涨，所以这四个调用点必须按它们各自的**上界**量：

| 调用点 | 输入上界从哪来 |
|---|---|
| rerank | RAG_RERANK_CANDIDATES × RAG_RERANK_SNIPPET_CHARS |
| memory_extract | question[:2000] + answer[:4000]（memory_service 里写死的截断） |
| query_condense | 最近 6 轮 × 400 字（chat_service._condense_query 的切片） |
| history_summary | **无上界**——滑出预算的历史有多少就压多少 |

最后一项是这里面唯一真正危险的：另外三个有硬截断，摘要没有。所以它按
HISTORY_TOKEN_BUDGET 量——一次能掉出窗口的历史大致就是这个量级。

跑法：cd back-end && python scripts/sweep_worst_case_budgets.py
每个调用点从小到大试，命中即停，所以正常情况下只花一两次调用。
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

BUDGETS = [1024, 2048, 3072, 4096, 6144]
QUESTION = "跨部门借调期间的绩效由哪一方考核，考核结果怎么合并？"

_SEED = [
    "借调期间的绩效考核由借入部门负责，考核结果需在考核周期结束后 5 个工作日内同步给借出部门，并由人力资源部归档。",
    "员工年度绩效由所在部门主管评定，评定结果计入年度调薪基数，调薪幅度由薪酬委员会在每年第一季度统一核定。",
    "差旅费报销须在费用发生后 30 个自然日内提交，超期需部门负责人书面说明，财务部有权对未说明的申请直接退单。",
    "跨部门协作项目的成果归属按项目立项书约定，未约定的由项目管理委员会裁定，裁定结果对各参与部门均有约束力。",
    "试用期考核不合格的，用人部门应在试用期结束前 10 日内提出书面意见，并同步至人力资源部启动后续流程。",
]


def _filler(index: int, length: int) -> str:
    """凑出指定长度的像样中文正文。

    重复填充而不是补空白：空白会被 tokenizer 压掉，量不出真实输入规模。
    """
    base = _SEED[index % len(_SEED)] + f"（条目 {index + 1}，补充说明。）"
    return (base * (length // len(base) + 1))[:length]


def _rerank_prompt() -> str:
    count = max(2, settings.RAG_RERANK_CANDIDATES)
    passages = [
        f"[{i}] {_filler(i - 1, settings.RAG_RERANK_SNIPPET_CHARS)}"
        for i in range(1, count + 1)
    ]
    return (
        "下面是候选参考片段。请按与问题的相关程度从高到低排序，"
        "完全不相关的片段直接省略。只输出片段编号组成的 JSON 数组，"
        "例如 [3, 1, 5]，不要任何解释。\n\n"
        f"问题：{QUESTION}\n\n" + "\n\n".join(passages)
    )


def _extract_prompt() -> str:
    # memory_service.extract 里的切片就是 [:2000] / [:4000]
    return prompt_library.render(
        "memory_extract",
        question=_filler(0, 2000),
        answer=_filler(1, 4000),
    )


def _condense_prompt() -> str:
    # chat_service._condense_query: history[-6:]，每条 content[:400]
    turns = "\n".join(
        f"{'user' if i % 2 == 0 else 'assistant'}: {_filler(i, 400)}"
        for i in range(6)
    )
    return prompt_library.render(
        "rag_query_condense", recent_turns=turns, question="那考核结果怎么合并？"
    )


def _summary_prompt() -> str:
    # 没有硬截断，按 HISTORY_TOKEN_BUDGET 的量级构造（4 字/token 粗算）
    transcript = "\n".join(
        f"{'user' if i % 2 == 0 else 'assistant'}: {_filler(i, 600)}"
        for i in range(max(2, settings.HISTORY_TOKEN_BUDGET * 4 // 600))
    )
    return prompt_library.render(
        "history_summary", flags={"has_previous": False}, previous="", transcript=transcript
    )


# (标签, 当前配置值, 配置项名, schema 或 None(纯文本), array, temperature, prompt)
CASES = [
    (
        "rerank(llm)",
        settings.RAG_RERANK_MAX_TOKENS,
        "RAG_RERANK_MAX_TOKENS",
        structured.RerankOrder,
        True,
        0.0,
        _rerank_prompt,
    ),
    (
        "memory_extract",
        settings.MEMORY_EXTRACT_MAX_TOKENS,
        "MEMORY_EXTRACT_MAX_TOKENS",
        structured.MemoryItems,
        True,
        0.0,
        _extract_prompt,
    ),
    (
        "query_condense",
        settings.RAG_CONDENSE_MAX_TOKENS,
        "RAG_CONDENSE_MAX_TOKENS",
        None,
        False,
        0.0,
        _condense_prompt,
    ),
    (
        "history_summary",
        settings.HISTORY_SUMMARY_MAX_TOKENS,
        "HISTORY_SUMMARY_MAX_TOKENS",
        None,
        False,
        0.2,
        _summary_prompt,
    ),
]


async def attempt(adapter, schema, array, temperature, prompt, budget):
    if schema is not None:
        result, report = await structured.request_structured(
            adapter,
            schema=schema,
            prompt=prompt,
            model=settings.utility_model,
            purpose="sweep",
            array=array,
            temperature=temperature,
            max_tokens=budget,
            retries=0,
        )
        raw = (report.last_raw or "").strip()
        return result is not None, raw, report.finish_reason
    completion = await adapter.complete(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        model=settings.utility_model,
        temperature=temperature,
        max_tokens=budget,
        purpose="sweep",
    )
    raw = (completion.content or "").strip()
    return bool(raw), raw, completion.finish_reason


async def main() -> None:
    adapter = OpenAICompatibleAdapter()
    print(f"辅助模型 = {settings.utility_model}\n")
    rows = []

    for label, current, key, schema, array, temperature, build in CASES:
        prompt = build()
        print(f"=== {label}   提示词 {len(prompt)} 字   当前 {key}={current}")
        first_ok = None
        for budget in BUDGETS:
            ok, raw, finish = await attempt(
                adapter, schema, array, temperature, prompt, budget
            )
            mark = "通过" if ok else "失败"
            empty = "，content 为空串" if not raw else ""
            print(
                f"  max_tokens={budget:<5} {mark}  输出 {len(raw)} 字{empty}  "
                f"finish_reason={finish}"
            )
            if ok:
                first_ok = budget
                break
        rows.append((label, key, current, first_ok))
        if first_ok is None:
            print(f"  → 扫到 {BUDGETS[-1]} 仍然失败\n")
        else:
            print(f"  → 最坏情况最低通过值 = {first_ok}\n")

    print("== 建议 ==")
    print("（留一倍余量：候选数、历史预算都是可配的，贴着最低值配等于把"
          "「改大那个配置」变成一个静默失效的开关）\n")
    for label, key, current, first_ok in rows:
        if first_ok is None:
            print(f" !! {label:<16} {key}: 扫不出通过值，需要单独查")
        elif current < first_ok:
            print(
                f" !! {label:<16} {key}={current} < 最低 {first_ok}"
                f"  → 改成 {first_ok * 2}（现在 100% 降级）"
            )
        elif current < first_ok * 2:
            print(
                f"  ~ {label:<16} {key}={current} 能过但余量不足"
                f"（最低 {first_ok}）→ 建议 {first_ok * 2}"
            )
        else:
            print(f"     {label:<16} {key}={current} 够用且有余量（最低 {first_ok}）")


asyncio.run(main())
