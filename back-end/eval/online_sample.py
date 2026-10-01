"""线上真实流量抽样送评：把生产里的真实问答回流进 judge 打分。

补的是"离线金标 ↔ 线上分布"那半环。离线 eval 跑在固定金标问题、温度 0 上；线上是
千奇百怪的真实问题、温度 0.7。两套分布，离线分数再好看也不等于线上就好。这里从
``messages`` 抽真实问答，用**同一个 AnswerJudge** 打分，看线上到底什么样，并把低分的
导成回归候选（同 ``from_feedback``，needs_review=true，另存一个文件不污染金标）。

两条诚实边界（报告与导出里都写明）：

1. **context 是现在重检索的，不是当初那条回答真正用的。** 原始 context 只活在那条
   SSE 回合里、没落库，所以这里拿当前知识库重检索一次喂给裁判。于是 **faithfulness
   判的是"对当前知识库忠不忠实"**——知识库改过就会漂；而 **relevance（答没答到点上）
   不吃 context，是准的**。读报告时以 relevance 为主、faithfulness 为参考。

2. **answerable 一律当 True。** 真实流量没有可答性标注，统一按可答判。对那些本该拒答
   的问题，这里的低分要连着"它是不是本就该拒答"一起看——所以导出的是**候选**，
   needs_review=true，人工复核时判。

用法：

```bash
python -m eval.online_sample --limit 50 --since-hours 168 --dry-run   # 先看分布
python -m eval.online_sample --limit 50 --export                      # 低分导成回归候选
python -m eval.run --dataset eval/datasets/online_sample_regression.jsonl
```

会真实调用 embedding（重检索）与裁判模型，产生费用——同 eval.run，不在 CI 上跑。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from eval import metrics
from services.clock import naive_now

_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(_EVAL_DIR, "datasets", "online_sample_regression.jsonl")

# 低于这个分（faithfulness / relevance 取较小值）才导成回归候选。
_DEFAULT_LOW_SCORE = 3.0
_KEYWORD_RE = re.compile(r"[A-Z][A-Z_]{3,}|\d+(?:\.\d+)?%?")


@dataclass(slots=True)
class Turn:
    question: str
    answer: str
    chat_id: str
    message_id: str
    user_id: str
    workspace_id: str | None


@dataclass(slots=True)
class Scored:
    turn: Turn
    faithfulness: float | None = None
    relevance: float | None = None
    failed: bool = False
    reason: str = ""


def recent_turns(session, *, limit: int, since_hours: float | None = None) -> list[Turn]:
    """最近的 (真实问题, 真实回答) 对。按 assistant 消息往前找同对话的那条 user 消息。

    seq 是全局自增唯一列，既是对话内顺序也近似全局时间序，用它配对最稳
    （created_at 的秒级精度会把同一回合的问答判成同时刻）。
    """
    from models import Chat, Message, User

    query = session.query(Message).filter(Message.role == "assistant")
    if since_hours:
        query = query.filter(Message.created_at >= naive_now() - timedelta(hours=since_hours))
    # 多取一些：有些 assistant 消息前面没有 user 消息（系统首条、脏数据），要能跳过补足
    assistants = query.order_by(Message.seq.desc()).limit(max(1, limit) * 3).all()

    turns: list[Turn] = []
    chat_cache: dict[str, Any] = {}
    ws_cache: dict[str, str | None] = {}
    for assistant in assistants:
        if len(turns) >= limit:
            break
        if not (assistant.content or "").strip():
            continue
        user_msg = (
            session.query(Message)
            .filter(
                Message.chat_id == assistant.chat_id,
                Message.role == "user",
                Message.seq < assistant.seq,
            )
            .order_by(Message.seq.desc())
            .first()
        )
        if user_msg is None or not (user_msg.content or "").strip():
            continue
        if assistant.chat_id not in chat_cache:
            chat_cache[assistant.chat_id] = session.get(Chat, assistant.chat_id)
        chat = chat_cache[assistant.chat_id]
        if chat is None:
            continue
        if chat.user_id not in ws_cache:
            owner = session.get(User, chat.user_id)
            ws_cache[chat.user_id] = owner.workspace_id if owner else None
        turns.append(
            Turn(
                question=user_msg.content,
                answer=assistant.content,
                chat_id=assistant.chat_id,
                message_id=assistant.id,
                user_id=chat.user_id,
                workspace_id=ws_cache[chat.user_id],
            )
        )
    return turns


async def judge_turns(session, turns, *, knowledge=None, judge=None, top_k: int = 5) -> list[Scored]:
    """逐条重检索 + 送裁判。单条异常收敛成 failed，不中断整批。"""
    if knowledge is None:
        from services.knowledge_service import KnowledgeService

        knowledge = KnowledgeService()
    if judge is None:
        from eval.judge import AnswerJudge
        from services.model_adapter import OpenAICompatibleAdapter

        judge = AnswerJudge(OpenAICompatibleAdapter())

    scored: list[Scored] = []
    for turn in turns:
        try:
            context = ""
            if turn.workspace_id:
                context, _cites = await knowledge.build_rag_context_with_citations(
                    session, turn.question, turn.workspace_id, top_k=top_k,
                    viewer_id=turn.user_id,
                )
            verdict = await judge.judge(
                question=turn.question, answer=turn.answer,
                context=context, answerable=True,
            )
        except Exception as exc:  # noqa: BLE001
            scored.append(Scored(turn=turn, failed=True, reason=f"judge_error:{type(exc).__name__}"))
            continue
        scored.append(Scored(
            turn=turn, faithfulness=verdict.faithfulness, relevance=verdict.relevance,
            failed=verdict.failed, reason=verdict.reason,
        ))
    return scored


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _low_score(scored: Scored, threshold: float) -> bool:
    """relevance 为主判据（不吃 context，准）；faithfulness 缺失时不参与。"""
    if scored.failed:
        return False
    candidates = [v for v in (scored.faithfulness, scored.relevance) if v is not None]
    return bool(candidates) and min(candidates) < threshold


def compute_report(scored: list[Scored], *, threshold: float = _DEFAULT_LOW_SCORE) -> dict[str, Any]:
    judged = [s for s in scored if not s.failed]
    faith = [s.faithfulness for s in judged if s.faithfulness is not None]
    rel = [s.relevance for s in judged if s.relevance is not None]
    low = [s for s in judged if _low_score(s, threshold)]
    return {
        "sampled": len(scored),
        "judged": len(judged),
        "failed": sum(1 for s in scored if s.failed),
        "threshold": threshold,
        "faithfulness": {
            "mean": _avg(faith), "p50": metrics.percentile(faith, 50),
            "min": min(faith) if faith else None,
        },
        "relevance": {
            "mean": _avg(rel), "p50": metrics.percentile(rel, 50),
            "min": min(rel) if rel else None,
        },
        "lowScorers": len(low),
    }


def _slug(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "-", text).strip("-").lower()
    if cleaned:
        return cleaned[:40]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return digest if text else fallback


def _existing_ids() -> set[str]:
    if not os.path.exists(OUTPUT_PATH):
        return set()
    ids: set[str] = set()
    with open(OUTPUT_PATH, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                ids.add(json.loads(line)["id"])
    return ids


def _to_case(scored: Scored) -> dict[str, Any]:
    """同 from_feedback 的回归用例形状：无来源标注、needs_review，另存一个文件。

    期望答案这里**留空**——线上抽样没有人工标注的正确答案，must_include 抠不出来
    （from_feedback 是从用户填的 expected_answer 抠的，这里没有）。人工复核时补。
    附上线上分数与原回答，复核的人据此判这到底是模型答差了还是问题本就该拒答。
    """
    turn = scored.turn
    return {
        "id": f"onl-{_slug(turn.question, turn.message_id[:8])}-{turn.message_id[:6]}",
        "probe": "online_sample",
        "answerable": True,
        "question": turn.question,
        "expected_documents": [],
        "must_include": [],
        "must_avoid": [],
        "reference_answer": "",
        "needs_review": True,
        "onlineFaithfulness": scored.faithfulness,
        "onlineRelevance": scored.relevance,
        "onlineAnswer": turn.answer[:2000],
    }


def export_low_scorers(
    scored: list[Scored], *, threshold: float = _DEFAULT_LOW_SCORE, dry_run: bool = False
) -> list[dict[str, Any]]:
    """把低分样本导成回归候选。按 id 去重（重跑不重复追加），dry_run 只返回不落盘。"""
    known = _existing_ids()
    cases: list[dict[str, Any]] = []
    for scored_turn in scored:
        if not _low_score(scored_turn, threshold):
            continue
        case = _to_case(scored_turn)
        if case["id"] in known:
            continue
        known.add(case["id"])
        cases.append(case)

    if dry_run or not cases:
        return cases
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "a", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    return cases


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def render(report: dict[str, Any]) -> str:
    faith, rel = report["faithfulness"], report["relevance"]
    return "\n".join([
        "# 线上抽样送评",
        "",
        f"抽样 {report['sampled']} 条 · 判成 {report['judged']} 条 · 裁判失败 {report['failed']} 条",
        f"低分（任一维度 < {report['threshold']}）：{report['lowScorers']} 条",
        "",
        "| 指标 | 均值 | 中位 | 最低 |",
        "| --- | --- | --- | --- |",
        f"| 相关性（准） | {_fmt(rel['mean'])} | {_fmt(rel['p50'])} | {_fmt(rel['min'])} |",
        f"| 忠实度（对当前库） | {_fmt(faith['mean'])} | {_fmt(faith['p50'])} | {_fmt(faith['min'])} |",
        "",
        "- **相关性不吃 context，是准的**；忠实度是对**当前**知识库判的，知识库变过会漂。",
        "- answerable 一律当 True：本该拒答的问题会被判低分，复核时连 abstention 一起看。",
        "- 这是线上真实分布，不是金标——用来回答「离线分数好，线上也好吗」。",
    ])


async def main() -> None:
    parser = argparse.ArgumentParser(description="线上真实问答抽样送评")
    parser.add_argument("--limit", type=int, default=50, help="最多抽样多少条（默认 50）")
    parser.add_argument("--since-hours", type=float, default=None, help="只抽最近 N 小时内的")
    parser.add_argument("--threshold", type=float, default=_DEFAULT_LOW_SCORE,
                        help=f"低于此分导成回归候选（默认 {_DEFAULT_LOW_SCORE}）")
    parser.add_argument("--top-k", type=int, default=5, help="重检索取多少块喂裁判")
    parser.add_argument("--export", action="store_true", help="把低分样本落盘成回归候选")
    parser.add_argument("--dry-run", action="store_true", help="只打印会导出什么，不落盘")
    args = parser.parse_args()

    from database import SessionLocal

    session = SessionLocal()
    try:
        turns = recent_turns(session, limit=args.limit, since_hours=args.since_hours)
        if not turns:
            print("没有可抽样的线上问答（messages 里没有成对的 user→assistant）。")
            return
        scored = await judge_turns(session, turns, top_k=args.top_k)
        report = compute_report(scored, threshold=args.threshold)
        print(render(report))

        if args.export or args.dry_run:
            cases = export_low_scorers(scored, threshold=args.threshold, dry_run=args.dry_run)
            if not cases:
                print("\n没有新的低分候选可导出。")
            elif args.dry_run:
                print(f"\n[dry-run] {len(cases)} 条低分候选（未落盘）：")
                for case in cases:
                    print(f"  {case['id']}  {case['question'][:50]}")
            else:
                print(f"\n已追加 {len(cases)} 条到 {OUTPUT_PATH}（needs_review=true）。")
                print(f"跑它们：python -m eval.run --dataset {OUTPUT_PATH}")
    finally:
        session.close()


if __name__ == "__main__":
    asyncio.run(main())
