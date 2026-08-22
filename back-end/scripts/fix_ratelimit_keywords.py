"""修 tool-ratelimit-batch 的 must_include。

原来写的是 ["60", "600"]，但 keyword_coverage 是纯子串匹配（metrics.py:103），
"60" 是 "600" 的子串——模型只答出默认额度 600、完全没提批量导入那一档，
关键词覆盖率照样是 100%。而这条用例的全部意义就在于区分两档。

60 的任何纯数字写法都是 600 的子串，所以改用 "批量" 这个词：它逼着答案真的
讨论批量导入那一档，与 "600" 一起才构成可区分的信号。
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

TARGET = "tool-ratelimit-batch"
NEW_KEYWORDS = ["批量", "600"]


def main() -> None:
    with io.open(DATASET, "r", encoding="utf-8") as handle:
        cases = [json.loads(line) for line in handle if line.strip()]

    hit = False
    for case in cases:
        if case["id"] != TARGET:
            continue
        old = case["turns"][0].get("must_include")
        case["turns"][0]["must_include"] = NEW_KEYWORDS
        print(f"{TARGET}: {old} -> {NEW_KEYWORDS}")
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
