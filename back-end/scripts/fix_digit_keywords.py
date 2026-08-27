"""把 9 条金标里的裸数字关键词换成"数字+量词"。

**为什么要改。** ``keyword_coverage`` 原来是纯子串匹配，``"3 个工作日"`` 匹配不上
模型写的 ``"3个工作日"``，于是金标只能退化成断言裸数字 ``"3"``。而裸数字会被同
一篇文档里更长的数字整体包含：

    leave-carryover     "5"  ⊂ "15 天"（陪产假）      问结转 5 天，答 15 天也满分
    expense-deadline    "30" ⊂ "300 元"（招待标准）
    alcohol-cap         "30" ⊂ "300 元"
    prod-write-window   "4"  ⊂ "14 天"（口令提醒）
    key-leak-sla        "1"  ⊂ "14 位"（口令长度）
    repo-access-sla     "1"  ⊂ "11"（Python 3.11）
    mentor-period       "3"  ⊂ "3000"（端口段）
    trial-period        "6"  ⊂ "2026 年"（施行日期）
    sick-leave-proof    "2"  ⊂ "2026 年"

30 条老题里 9 条中招，占 30%。

**前提**：``eval/metrics.py`` 的 ``keyword_coverage`` 已改为先 ``_fold``
（小写 + 去全部空白）再比对。没有这一步的话换成 ``"5 天"`` 会变成恒不命中——
那比假满分更糟，因为它把好答案判成坏答案。

**换词原则**：优先"数字+量词"，因为它同时锁住数值和单位；量词不唯一时（比如
``"1 小时"`` 与 ``"1 个工作日"`` 在同篇共存）补一个该条独有的名词。

用法：

    python scripts/fix_digit_keywords.py --dry-run
    python scripts/fix_digit_keywords.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
GOLDEN = os.path.join(_BACKEND, "eval", "datasets", "rag_golden.jsonl")
CORPUS = os.path.join(_BACKEND, "eval", "corpus")

# id -> 新的 must_include。每条都在下面注明为什么这么选。
FIXES: dict[str, list[str]] = {
    # "6 个月"锁住试用期长度；"2026" 折叠后是 "2026"，不含 "6个月"
    "trial-period": ["6 个月"],
    # 合成一个词而不是 ["结转","5 天"]：单独的 "5 天" 折叠后会被同篇"陪产假
    # 15 天"的 "15天" 包含。原文是"最多结转 5 天到次年"，折叠成"结转5天"之后
    # 任何关于 15 天的答案都构不出这个串，一个关键词就锁住了数值和语境。
    "leave-carryover": ["结转 5 天"],
    # "2 天"会被"12 天"（年假 3-5 年）包含，改用"连续病假"锁定条件本身，
    # 再用"10 天"锁上限（"10 天"不被同篇任何更长数字包含）
    "sick-leave-proof": ["连续病假", "10 天"],
    # "30 个自然日"是原文写法，唯一；"90 天"不被包含
    "expense-deadline": ["30 个自然日", "90 天"],
    # 百分号让它唯一——"300 元"折叠后是"300元"，不含"30%"
    "alcohol-cap": ["30%"],
    # "4 小时"唯一（同篇的 14 是"14 位"和"14 天"）
    "prod-write-window": ["4 小时"],
    # "1 小时"配"吊销"：同篇 "180 天" 里没有 "1小时"，但加一个动词更稳
    "key-leak-sla": ["1 小时", "吊销"],
    # "1 个工作日"是表格原文；"11"（Python 3.11）折叠后不含它
    "repo-access-sla": ["1 个工作日"],
    # "3 个月"锁周期；"3000"（端口段）折叠后不含"3个月"
    "mentor-period": ["3 个月"],
}


def _fold(text: str) -> str:
    """与 eval/metrics.py 的 _fold 保持一致。两处不一致会让校验通过但实测失败。"""
    return "".join(text.lower().split())


def _load_corpus() -> dict[str, str]:
    return {
        name: open(os.path.join(CORPUS, name), encoding="utf-8").read()
        for name in os.listdir(CORPUS)
        if name.endswith(".md")
    }


def check(new_kws: list[str], docs: list[str], corpus: dict[str, str]) -> list[str]:
    """新关键词必须：在语料里折叠后真实出现，且不被更长的数字串整体包含。"""
    errors: list[str] = []
    for kw in new_kws:
        folded = _fold(kw)
        if not any(folded in _fold(corpus.get(d, "")) for d in docs):
            errors.append(f"{kw!r} 在 {docs} 里折叠后找不到")
            continue
        # 数字子串检查：把语料里所有"数字+后续非空白字符"的片段折叠出来比
        for d in docs:
            body = _fold(corpus.get(d, ""))
            for m in re.finditer(re.escape(folded), body):
                # 命中位置前面紧跟数字 => 说明它是更长数字的尾部
                if m.start() > 0 and body[m.start() - 1].isdigit():
                    errors.append(
                        f"{kw!r} 在 {d} 里被更长的数字包含"
                        f"（前一个字符是 {body[m.start()-1]!r}）"
                    )
                    break
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    corpus = _load_corpus()
    rows = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    byid = {r["id"]: r for r in rows}

    missing = [cid for cid in FIXES if cid not in byid]
    if missing:
        print(f"这些 id 不在金标里: {missing}")
        return 1

    errors: list[str] = []
    plan = []
    for cid, new_kws in FIXES.items():
        row = byid[cid]
        docs = row.get("expected_documents", [])
        errs = check(new_kws, docs, corpus)
        if errs:
            errors.extend(f"{cid}: {e}" for e in errs)
        plan.append((cid, row["must_include"], new_kws))

    print(f"金标 {len(rows)} 条，本次改 {len(FIXES)} 条\n")
    for cid, old, new in plan:
        print(f"  {cid:26} {old} -> {new}")

    if errors:
        print(f"\n校验失败 {len(errors)} 项：")
        for e in errors:
            print(f"  x {e}")
        return 1

    print("\n校验通过。")
    if args.dry_run:
        print("--dry-run，未写入。")
        return 0

    for cid, new_kws in FIXES.items():
        byid[cid]["must_include"] = new_kws
    with open(GOLDEN, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"已写入 {GOLDEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
