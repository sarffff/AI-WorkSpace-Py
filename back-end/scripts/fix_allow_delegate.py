# -*- coding: utf-8 -*-
"""把 `delegate` 加进每一轮的 allow_tools。

## 这是在修一个测量偏差，不是放宽标准

`delegation-augment` / `delegation-supervisor` 两个变体给主代理多一个 `delegate`
工具。而 `agent_runner` 把子代理的工具调用和 `delegate` 本身都记进 `calls`
（那是对的，见那边对 `agent_step` 的处理），于是：

    tool_precision = 落在 expect+allow 里的调用数 / 总调用数

数据集 27 轮里有 23 轮标了 expect/allow，**其中列了 `delegate` 的是 0 轮**。
也就是说委派变体每委派一次，精度分母就多一、分子不变——模型越是正确使用这个
功能，分数越低。报告上读出来会是"委派让工具精度下降"，而那是数据集造成的，
不是委派造成的。

这和这个仓库里已经踩过的几次是同一形状：

  - 8 条用例把 `search_knowledge_base` 标成 expect，而 RAG_PREFETCH 让它在
    round 0 就跑完了 → 召回恒 0，读作"模型不会检索"
  - 5 个 RAG 变体的增强调用因为 max_tokens 太小 100% 返回空串 → 与 baseline
    逐位相同，读作"这个技术没有增益"
  - `injectionResistRate` 把 recovery 探针算进分母 → 0.5，读作"防线漏了一半"

每次都是**尺子坏了，而坏掉的方式让结论看起来很合理**。

## 为什么是 allow 而不是 expect

`delegate` 不是任何一条用例"必须"做的动作——那是委派策略该自己判断的事。列进
allow 只表示"这么做不算噪声"，收益仍然要由任务成功率、轮次和成本体现。放进
expect 会变成强迫单代理变体去调一个它根本没有的工具。

## 为什么不改 tool_precision 的实现去特判 delegate

试过想在指标里把 `delegate` 排除掉，但那样会把"委派了但一次都没用上"这种真实
浪费也一起藏掉。允许清单是显式的、逐轮可见的，指标里的特判是隐式的。
"""

from __future__ import annotations

import json
from pathlib import Path

DATASET = Path(__file__).resolve().parents[1] / "eval" / "datasets" / "agent_tasks.jsonl"


def main() -> None:
    lines = DATASET.read_text(encoding="utf-8").splitlines()
    cases = [json.loads(line) for line in lines if line.strip()]

    touched: list[str] = []
    for case in cases:
        changed = False
        for turn in case["turns"]:
            allow = list(turn.get("allow_tools") or [])
            expect = list(turn.get("expect_tools") or [])
            # 只补标了工具期望的轮次。expect 和 allow 都空表示这一轮不看工具指标
            # （tool_precision 对空期望返回 None），补上去没有意义。
            if not allow and not expect:
                continue
            if "delegate" in allow or "delegate" in expect:
                continue
            allow.append("delegate")
            turn["allow_tools"] = allow
            changed = True
        if changed:
            touched.append(case["id"])

    if not touched:
        print("没有需要改的，已是目标状态。")
        return

    DATASET.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n",
        encoding="utf-8",
    )
    print(f"补了 {len(touched)} 条用例的 allow_tools：")
    for case_id in touched:
        print(f"  {case_id}")


if __name__ == "__main__":
    main()
