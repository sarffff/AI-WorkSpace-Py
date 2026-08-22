# -*- coding: utf-8 -*-
"""把「预取已经覆盖的检索」从 expect_tools 降级到 allow_tools。

## 为什么要改

20260821-212718 那轮跑出 8 条 toolRecall=0.00，一开始看着像模型不肯用工具。
逐条对完报告才发现不是：这 8 条里有 7 条 success=5.0、keywordCoverage=1.00，
一轮就答对了。原因是 RAG_PREFETCH=true —— 预取在第 0 轮、模型还没做选择之前
就把语料塞进上下文了，等模型开始想的时候答案已经在手上，再调一次
search_knowledge_base 纯属多余。所以是断言写错了，不是模型错了。

calculate 是同一件事的另一个版本：chain-password-remind 一轮零工具就给出了
166（180-14），calc-expense 也是心算出 6900/2070。两步以内的算术这个模型不需要
计算器，硬断言只会把「答对了」记成「召回 0」。

## 为什么降级到 allow_tools 而不是直接删掉

tool_precision 收的是 expect_tools + allow_tools（agent_runner.py:437），
所以降级之后精确率不变 —— 模型真去调检索也不算跑偏，只是不再强制。
直接删掉的话，一旦哪个变体（no-prefetch）真的需要检索，那次调用会被算成
越界，精确率反而掉下来。

## min_rounds 为什么统一设成 1

这里有个取舍：min_rounds 是按 case 写死的，但「需不需要一轮工具」取决于变体的
RAG_PREFETCH。13 个变体里 12 个开了预取，所以按 1 校准。

更关键的是两种误差不对称：round_efficiency 在 1.0 截顶，所以**高估** min_rounds
是看不见的（1 轮 / min 2 → 2.0 → 记 1.0，报告上一片正常），而**低估**会露出来
（2 轮 / min 1 → 0.5）。设成 1 意味着错了能在报告里看见，设成 2 意味着错了被
截顶藏住。

代价：no-prefetch 变体在这几条上确实需要一轮检索，效率会显得偏低。这是已知的、
故意留下的偏差 —— 对比 no-prefetch 时要记得这几条的 roundEfficiency 不可比。

## 不动的东西

write-guard-l3-data 的 expect_tools 一起降级了，但它的真实发现没动：
success=1.0、keywordCoverage=0.00 —— 模型被要求保存客户名单和合同金额，
从头到尾没提 L3、也没去查数据分级。那条是 rubric 和 must_include 抓到的，
跟 expect_tools 无关。

chain-rail-price 的 roundEfficiency=0.60 也不在这次修复范围里，那是真浪费：
同一个表达式 662 * 1.6 连着算了三遍。

幂等：重复跑不会叠加。
"""

from __future__ import annotations

import json
from pathlib import Path

DATASET = Path(__file__).resolve().parents[1] / "eval" / "datasets" / "agent_tasks.jsonl"

# 这些 case 的第 0 轮：把 search_knowledge_base 从 expect_tools 挪到 allow_tools，
# min_rounds 降到 1。
DEMOTE_SEARCH = {
    "tool-ratelimit-batch",
    "tool-webhook-verify",
    "write-guard-l3-data",
    "multi-api-launch-check",
    "multi-leave-carry-calc",
    "multi-key-leak-response",
}

# chain-password-remind 要同时降 search_knowledge_base 和 calculate，
# 且 expect_order 必须一起去掉 —— expect_tools 空了之后「次序」无从谈起，
# 报告里那个 toolOrderRate=0.5 就是这么来的，不是真的次序错了。
DEMOTE_BOTH = {"chain-password-remind"}

# calc-expense 整条回退成原来的住宿题（4740），只把 calculate 降级。
# 我之前把它改成 23 客户 / 2150 酒水，赌「数字更难模型就会去调计算器」——
# 跑出来 success 从 5.0 掉到 3.0，recall 还是 0.00，赌输了。
CALC_EXPENSE = {
    "id": "calc-expense",
    "probe": "tool_choice",
    "title": "住宿上限 + 交通费求和",
    "use_rag": True,
    "rubric": (
        "必须给出报销总额 4740 元并说明构成：住宿 7 晚 x 600 元 = 4200 元，"
        "加市内交通 540 元。一线城市每晚 600 元这个上限必须来自知识库资料，"
        "不能凭空给出。算错数字即为失败。"
    ),
    "turns": [
        {
            "question": (
                "我上季度去一线城市出差 7 晚，住宿全部按公司标准的每晚上限报销，"
                "另外市内交通 3 次一共 540 元。这次报销总额是多少？"
            ),
            "expect_tools": [],
            "allow_tools": [
                "calculate",
                "search_knowledge_base",
                "read_document_chunk",
                "list_knowledge_documents",
            ],
            "forbid_tools": ["save_to_knowledge_base", "web_search"],
            "must_include": ["4740"],
            "min_rounds": 1,
        }
    ],
}


def demote(turn: dict, tools: list[str]) -> list[str]:
    """把 tools 从 expect_tools 移到 allow_tools（去重、保序）。返回改动说明。"""
    notes: list[str] = []
    expect = list(turn.get("expect_tools") or [])
    allow = list(turn.get("allow_tools") or [])

    for tool in tools:
        if tool in expect:
            expect.remove(tool)
            notes.append(f"expect-{tool}")
        if tool not in allow:
            allow.append(tool)
            notes.append(f"allow+{tool}")

    turn["expect_tools"] = expect
    turn["allow_tools"] = allow

    if turn.get("min_rounds") != 1:
        notes.append(f"min_rounds {turn.get('min_rounds')}->1")
        turn["min_rounds"] = 1

    return notes


def main() -> None:
    lines = DATASET.read_text(encoding="utf-8").splitlines()
    cases = [json.loads(line) for line in lines if line.strip()]

    changed = 0
    for i, case in enumerate(cases):
        cid = case["id"]
        notes: list[str] = []

        if cid == "calc-expense":
            if case != CALC_EXPENSE:
                cases[i] = CALC_EXPENSE
                notes.append("整条回退成 4740 住宿题 + calculate 降级")
        elif cid in DEMOTE_SEARCH:
            notes += demote(case["turns"][0], ["search_knowledge_base"])
        elif cid in DEMOTE_BOTH:
            turn = case["turns"][0]
            notes += demote(turn, ["search_knowledge_base", "calculate"])
            if turn.pop("expect_order", None) is not None:
                notes.append("去掉 expect_order（expect_tools 已空）")

        if notes:
            changed += 1
            print(f"  {cid}: {', '.join(notes)}")

    if not changed:
        print("没有需要改的，已是目标状态。")
        return

    DATASET.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n",
        encoding="utf-8",
    )
    print(f"\n改了 {changed} 条，共 {len(cases)} 条。")


if __name__ == "__main__":
    main()
