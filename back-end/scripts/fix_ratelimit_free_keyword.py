# -*- coding: utf-8 -*-
"""tool-ratelimit-batch：去掉「批量」这个白送的关键词。

## 问题

must_include 是 ["批量", "600"]，而「批量」两个字就在题干里
（"我们的批量导入接口老是返回 429"）。模型只要复述一下问题就拿到 50% 覆盖率，
这个关键词不携带任何信息。

## 为什么不换成 "60次" 之类

这条题真正要判的是「有没有给出两个不同的档位」（默认 600/分，批量 60/分），
但 keyword_coverage 是纯子串匹配、不做空白归一化（metrics.py:103），
表达这件事的路全是坑：

  - "60"    → 是 "600" 的子串，只答默认档也算过（这就是上一版的 bug）
  - "60次"  → 语料原文写的是「每分钟 60 次」带空格，模型写带空格就漏判
  - "60 次" → 这次的答案写的是不带空格的 "60次"，同样漏判
  - "1/10"  → 是模型自己这轮的措辞，不是语料事实，照它写等于过拟合单次结果

所以退回到只留 "600"（语料事实、无歧义、不与别的关键词互为子串），
「两个档位」这件事交给 rubric —— 它本来就写着「只给一个数字、或者把两者混成
同一个额度都算失败」，裁判读全文判这个比子串匹配可靠。

这不是把断言放宽，是把它挪到能正确表达它的地方。

幂等：重复跑不会叠加。
"""

from __future__ import annotations

import json
from pathlib import Path

DATASET = Path(__file__).resolve().parents[1] / "eval" / "datasets" / "agent_tasks.jsonl"
TARGET = "tool-ratelimit-batch"


def main() -> None:
    lines = DATASET.read_text(encoding="utf-8").splitlines()
    cases = [json.loads(line) for line in lines if line.strip()]

    changed = False
    for case in cases:
        if case["id"] != TARGET:
            continue
        turn = case["turns"][0]
        before = list(turn.get("must_include") or [])
        after = [k for k in before if k != "批量"]
        if before != after:
            turn["must_include"] = after
            changed = True
            print(f"  {TARGET}: must_include {before} -> {after}")

    if not changed:
        print("没有需要改的，已是目标状态。")
        return

    DATASET.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n",
        encoding="utf-8",
    )
    print(f"\n改了 1 条，共 {len(cases)} 条。")


if __name__ == "__main__":
    main()
