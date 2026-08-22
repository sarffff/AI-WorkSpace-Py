"""重写 calc-expense 的题目，让 calculate 真的成为必要工具。

**为什么要改。** 2026-08-21 冒烟跑出 toolRecall 0.0 而 success 5.0：原题是
「一线城市 7 晚 × 每晚上限 600 + 市内交通 540」，模型直接心算出 4740，答案完全
正确，裁判给 5 分。于是这条用例惩罚了一个正确行为——600×7+540 本来就不需要
计算器，是数据集「正确答案至少需要 calculate」这个前提错了。

而 roundEfficiency 看不见这件事：rounds 1 / minRounds 2 算出 2.0 被上限截成
1.0，那个指标只能发现绕远路，发现不了数据集把最少轮次估多了。

**改法。** 保留「该用工具时用工具」这个测试意图（tool_choice 探针里只有这一条
测算术），换成心算不动的数：招待预算 23 × 300 = 6900，酒水上限 30% = 2070，
实际 2150 超标 80 元。2070 这个数猜不出来，必须真算。同时 6900 落在
5000–20000 区间，审批链路要追加财务负责人——这一半仍然考检索。

关键词 6900 / 2070 互不为子串（metrics.py:103 是纯子串匹配）。
"""
from __future__ import annotations

import io
import json
import os

DATASET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "eval",
    "datasets",
    "agent_tasks.jsonl",
)

TARGET = "calc-expense"

NEW_TITLE = "招待费占比 + 审批层级"
NEW_RUBRIC = (
    "必须算出三件事：按客户招待人均 300 元、23 人的预算是 6900 元；酒水上限为招待"
    "总额的 30%，也就是 2070 元；实际酒水 2150 元已经超标 80 元。并且要指出 6900 元"
    "落在 5000 至 20000 元区间，审批链路需要追加财务负责人（即直属主管 + 部门负责人"
    "+ 财务负责人）。占比或上限算错、或者漏掉超标这个结论，都算失败。"
    "人均标准、30% 上限与审批分档都必须来自知识库资料，不能凭常识给。"
)
NEW_QUESTION = (
    "上周招待了 23 位客户，按公司人均标准这次预算应该是多少？"
    "这次酒水一共花了 2150 元，超标了吗？另外这笔报销要走到哪一级审批？"
)
NEW_KEYWORDS = ["6900", "2070"]


def main() -> None:
    with io.open(DATASET, "r", encoding="utf-8") as handle:
        cases = [json.loads(line) for line in handle if line.strip()]

    hit = False
    for case in cases:
        if case["id"] != TARGET:
            continue
        turn = case["turns"][0]
        print(f"旧题：{turn['question'][:40]}...")
        print(f"旧关键词：{turn.get('must_include')}")
        case["title"] = NEW_TITLE
        case["rubric"] = NEW_RUBRIC
        turn["question"] = NEW_QUESTION
        turn["must_include"] = NEW_KEYWORDS
        # expect_tools / min_rounds 不变：calculate 仍是必要工具，
        # 「一轮工具 + 一轮作答」仍是下限。
        print(f"新题：{NEW_QUESTION[:40]}...")
        print(f"新关键词：{NEW_KEYWORDS}")
        hit = True

    if not hit:
        print(f"未找到 {TARGET}")
        return

    with io.open(DATASET, "w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"已重写，共 {len(cases)} 条")


if __name__ == "__main__":
    main()
