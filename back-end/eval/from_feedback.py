"""把线上差评导出成离线回归用例。

这是反馈闭环里最容易被省掉、也最值钱的一步:没有它,点踩只是个计数器;有了它,
每一次"这个答案不对"都变成下次改配置时必须重新跑过的用例。

三条刻意的限制:

1. **不写进 ``rag_golden.jsonl``。** 自动导出的样本没有来源标注,进不了检索指标;
   混进精心标注的金标准集里会污染 recall/nDCG 的可比性。另存一个文件,
   用 ``--dataset`` 指定跑哪一份。
2. **只导出差评。** 好评不构成回归用例——"这次答对了"不等于"以后必须这么答"。
3. **标 ``needs_review``。** 用户随手点的踩不一定是模型错(也可能是问题本身歧义)。
   导出的是**候选**,人工确认过再当基准。

用法:

```bash
python -m eval.from_feedback --dry-run          # 先看会导出什么
python -m eval.from_feedback                    # 落盘并标记已导出
python -m eval.run --dataset eval/datasets/feedback_regression.jsonl
```
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from typing import Any

from database import SessionLocal
from models import MessageFeedback
from services.clock import naive_now

_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(_EVAL_DIR, "datasets", "feedback_regression.jsonl")

# 从期望答案里抠出数字与代码样式的 token 当 must_include。
# 这只是省一点人工,不是自动标注:抠出来的关键词一样要人复核。
_KEYWORD_RE = re.compile(r"[A-Z][A-Z_]{3,}|\d+(?:\.\d+)?%?")


def _slug(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "-", text).strip("-").lower()
    if cleaned:
        return cleaned[:40]
    # 纯中文提问抠不出 ascii slug，用问题内容的短哈希保证 id 稳定且可区分
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return digest if text else fallback


def _keywords(expected: str | None) -> list[str]:
    if not expected:
        return []
    seen: list[str] = []
    for match in _KEYWORD_RE.findall(expected):
        if match not in seen:
            seen.append(match)
    return seen[:4]


def _existing_ids() -> set[str]:
    if not os.path.exists(OUTPUT_PATH):
        return set()
    ids: set[str] = set()
    with open(OUTPUT_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                ids.add(json.loads(line)["id"])
    return ids


def _to_case(feedback: MessageFeedback) -> dict[str, Any]:
    return {
        "id": f"fb-{_slug(feedback.question or '', feedback.id[:8])}-{feedback.id[:6]}",
        "probe": "feedback",
        "answerable": True,
        "question": feedback.question,
        # 线上反馈没有来源标注,留空表示"不参与检索指标"
        "expected_documents": [],
        "must_include": _keywords(feedback.expected_answer),
        "must_avoid": [],
        "reference_answer": feedback.expected_answer or "",
        # 人工复核前不要当基准使用
        "needs_review": True,
        "feedbackReason": feedback.reason,
        "feedbackComment": feedback.comment,
    }


def export(limit: int | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
    """导出未处理的差评。返回新增的用例列表。"""
    session = SessionLocal()
    try:
        query = (
            session.query(MessageFeedback)
            .filter(
                MessageFeedback.rating == "down",
                MessageFeedback.exported_at.is_(None),
                MessageFeedback.question.isnot(None),
            )
            .order_by(MessageFeedback.created_at.asc())
        )
        rows = query.limit(limit).all() if limit else query.all()
        if not rows:
            return []

        known = _existing_ids()
        cases: list[dict[str, Any]] = []
        exported: list[MessageFeedback] = []
        for row in rows:
            case = _to_case(row)
            if case["id"] in known:
                # 同一条反馈改过内容会重新置空 exported_at,这里只更新标记不重复追加
                exported.append(row)
                continue
            known.add(case["id"])
            cases.append(case)
            exported.append(row)

        if dry_run:
            return cases

        if cases:
            os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
            with open(OUTPUT_PATH, "a", encoding="utf-8", newline="\n") as handle:
                for case in cases:
                    handle.write(json.dumps(case, ensure_ascii=False) + "\n")

        now = naive_now()
        for row in exported:
            row.exported_at = now
        session.commit()
        return cases
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="把线上差评导出为回归用例")
    parser.add_argument("--limit", type=int, default=None, help="最多导出多少条")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印会导出什么，不落盘、不标记已导出",
    )
    args = parser.parse_args()

    cases = export(limit=args.limit, dry_run=args.dry_run)
    if not cases:
        print("没有待导出的差评。")
        return

    for case in cases:
        print(f"  {case['id']}  {case['question'][:50]}")
    if args.dry_run:
        print(f"\n[dry-run] 共 {len(cases)} 条，未写入。")
        return
    print(f"\n已追加 {len(cases)} 条到 {OUTPUT_PATH}")
    print("这些是候选用例，needs_review=true。人工确认 reference_answer 之后再当基准。")
    print(f"跑它们：python -m eval.run --dataset {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
