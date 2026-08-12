"""评估执行器。

一次运行的形状：

    对每个配置变体：
        （分块配置变了就重建索引）
        对每个问题：
            检索 -> 算检索指标
            生成答案 -> LLM 裁判打分
            从 trace 里取出本题的 token / 成本 / 耗时
        汇总成一行

关键取舍：这里评的是 **RAG 问答链路**（检索 + 单次生成），不是完整的 Agent 循环。
Agent 循环涉及多轮工具决策，方差大、成本高，适合单独设一套端到端评估；
把两者混在一起会让「检索改动到底有没有用」变得无法归因。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from config import settings
from database import SessionLocal
from eval import metrics
from eval.judge import AnswerJudge, JudgeVerdict
from eval.variants import Variant
from models import Document
from services.knowledge_service import KnowledgeService
from services.model_adapter import OpenAICompatibleAdapter
from services.pricing import estimate_cost
from services.retrieval_index import invalidate_user_indexes
from services.telemetry import tracer

logger = logging.getLogger("eval.runner")

_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
CORPUS_DIR = os.path.join(_EVAL_DIR, "corpus")
DATASET_PATH = os.path.join(_EVAL_DIR, "datasets", "rag_golden.jsonl")

# 评估语料挂在这个固定的伪用户下，和真实用户数据完全隔离
EVAL_USER_ID = "eval-harness"

_ANSWER_PROMPT = """你是知识库问答助手。只依据下面的参考内容回答问题。
参考内容里没有的信息，直接说明未找到，不要用你自己的知识补充，也不要猜测。
回答简洁准确，并标明来源文档名。

[参考内容]
{context}

[问题]
{question}"""


@dataclass(slots=True)
class QuestionCase:
    id: str
    question: str
    expected_documents: list[str]
    reference_answer: str = ""
    must_include: list[str] = field(default_factory=list)
    probe: str = "general"
    answerable: bool = True


@dataclass(slots=True)
class QuestionResult:
    case: QuestionCase
    retrieved_documents: list[str]
    retrieval: dict[str, float]
    answer: str
    keyword_coverage: float
    verdict: JudgeVerdict
    prompt_tokens: int
    completion_tokens: int
    cost: float | None
    currency: str | None
    latency_ms: int
    retrieval_ms: int


def load_cases(limit: int | None = None) -> list[QuestionCase]:
    cases: list[QuestionCase] = []
    with open(DATASET_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            cases.append(
                QuestionCase(
                    id=raw["id"],
                    question=raw["question"],
                    expected_documents=list(raw.get("expected_documents") or []),
                    reference_answer=raw.get("reference_answer", ""),
                    must_include=list(raw.get("must_include") or []),
                    probe=raw.get("probe", "general"),
                    answerable=bool(raw.get("answerable", True)),
                )
            )
    return cases[:limit] if limit else cases


def _chunking_fingerprint() -> str:
    """分块结果只由这几个设置决定；它们不变就不必重新索引。"""
    payload = json.dumps(
        {
            "chunk_max": settings.CHUNK_MAX_TOKENS,
            "chunk_overlap": settings.CHUNK_OVERLAP_TOKENS,
            "counter": settings.TOKEN_COUNTER,
            "embedding": settings.EMBEDDING_MODEL,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


async def ensure_corpus(knowledge: KnowledgeService) -> tuple[int, bool]:
    """确保评估语料已按当前分块配置索引好。

    文档名里带上分块指纹，于是「换了分块配置」自动表现为「另一批文档」：
    旧的删掉、新的重建，不会出现「测的是上一个配置留下的索引」这种静默错误。
    返回 (分块总数, 是否重建过)。
    """
    fingerprint = _chunking_fingerprint()
    session = SessionLocal()
    try:
        existing = (
            session.query(Document).filter(Document.user_id == EVAL_USER_ID).all()
        )
        current = [doc for doc in existing if doc.name.endswith(f"#{fingerprint}")]
        stale = [doc for doc in existing if not doc.name.endswith(f"#{fingerprint}")]

        files = sorted(
            name for name in os.listdir(CORPUS_DIR) if name.endswith(".md")
        )
        indexed_ok = [doc for doc in current if doc.status == "indexed"]
        if len(indexed_ok) == len(files) and not stale:
            return sum(doc.chunks for doc in indexed_ok), False

        for doc in stale + [doc for doc in current if doc.status != "indexed"]:
            session.delete(doc)
        session.commit()
        invalidate_user_indexes(EVAL_USER_ID)

        total_chunks = 0
        for name in files:
            with open(os.path.join(CORPUS_DIR, name), "rb") as handle:
                content = handle.read()
            document = await knowledge.upload_document(
                session, f"{name}#{fingerprint}", content, EVAL_USER_ID
            )
            total_chunks += document.chunks
        return total_chunks, True
    finally:
        session.close()


def _document_label(name: str) -> str:
    """去掉分块指纹后缀，还原成金标准里标注的文件名。"""
    return name.split("#", 1)[0]


def _span_totals(trace: Any) -> tuple[int, int, float | None, str | None]:
    """把一棵 trace 的 token 与成本加总。成本按币种分别累加，混币时不合并。"""
    if trace is None:
        return 0, 0, None, None
    prompt = sum(span.prompt_tokens or 0 for span in trace.spans)
    completion = sum(span.completion_tokens or 0 for span in trace.spans)

    by_currency: dict[str, float] = {}
    for span in trace.spans:
        cost = estimate_cost(span.model, span.prompt_tokens, span.completion_tokens)
        if cost is None:
            continue
        by_currency[cost.currency] = by_currency.get(cost.currency, 0.0) + float(
            cost.amount
        )
    if not by_currency:
        return prompt, completion, None, None
    # 单币种是常态；真出现多币种就只报最大的那个并在报告里注明局限
    currency = max(by_currency, key=lambda key: by_currency[key])
    return prompt, completion, by_currency[currency], currency


async def _run_case(
    knowledge: KnowledgeService,
    judge: AnswerJudge,
    adapter: Any,
    case: QuestionCase,
    top_k: int,
) -> QuestionResult:
    session = SessionLocal()
    try:
        async with tracer.trace(user_id=EVAL_USER_ID, chat_id=f"eval:{case.id}") as trace:
            started = time.perf_counter()
            retrieval_started = time.perf_counter()
            context, citations = await knowledge.build_rag_context_with_citations(
                session, case.question, EVAL_USER_ID, top_k=top_k
            )
            retrieval_ms = int((time.perf_counter() - retrieval_started) * 1000)

            ranked = [_document_label(item["document_name"]) for item in citations]
            relevant = set(case.expected_documents)
            retrieval_scores = metrics.evaluate_ranking(ranked, relevant, k=top_k)

            answer = ""
            try:
                completion = await adapter.complete(
                    messages=[
                        {
                            "role": "user",
                            "content": _ANSWER_PROMPT.format(
                                context=context or "（无参考内容）",
                                question=case.question,
                            ),
                        }
                    ],
                    tools=[],
                    model=settings.LLM_MODEL,
                    temperature=0.0,
                    max_tokens=800,
                    purpose="eval_answer",
                )
                answer = completion.content or ""
            except Exception as exc:
                logger.warning(
                    "answer generation failed for %s: %s", case.id, type(exc).__name__
                )

            verdict = await judge.judge(
                question=case.question,
                answer=answer,
                context=context,
                answerable=case.answerable,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)

        prompt_tokens, completion_tokens, cost, currency = _span_totals(trace)
        return QuestionResult(
            case=case,
            retrieved_documents=ranked,
            retrieval=retrieval_scores,
            answer=answer,
            keyword_coverage=metrics.keyword_coverage(answer, case.must_include),
            verdict=verdict,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            currency=currency,
            latency_ms=latency_ms,
            retrieval_ms=retrieval_ms,
        )
    finally:
        session.close()


def summarize(variant: Variant, results: list[QuestionResult]) -> dict[str, Any]:
    """把逐题结果汇总成一行。裁判失败的样本从均值里剔除而不是当 0 分。"""
    answerable = [r for r in results if r.case.answerable]
    absent = [r for r in results if not r.case.answerable]
    top_k = int(variant.overrides.get("RAG_TOP_K", settings.RAG_TOP_K))

    graded = [r for r in answerable if not r.verdict.failed]
    abstention_graded = [r for r in absent if not r.verdict.failed]

    summary: dict[str, Any] = {
        "variant": variant.name,
        "description": variant.description,
        "questions": len(results),
        f"recall@{top_k}": metrics.mean(
            [r.retrieval[f"recall@{top_k}"] for r in answerable]
        ),
        f"ndcg@{top_k}": metrics.mean(
            [r.retrieval[f"ndcg@{top_k}"] for r in answerable]
        ),
        "mrr": metrics.mean([r.retrieval["mrr"] for r in answerable]),
        "keywordCoverage": metrics.mean([r.keyword_coverage for r in answerable]),
        "faithfulness": metrics.mean(
            [r.verdict.faithfulness for r in graded if r.verdict.faithfulness is not None]
        ),
        "relevance": metrics.mean(
            [r.verdict.relevance for r in graded if r.verdict.relevance is not None]
        ),
        "abstentionRate": metrics.mean(
            [1.0 if r.verdict.abstained else 0.0 for r in abstention_graded]
        ),
        "judgeFailures": sum(1 for r in results if r.verdict.failed),
        "promptTokens": sum(r.prompt_tokens for r in results),
        "completionTokens": sum(r.completion_tokens for r in results),
        "avgLatencyMs": metrics.mean([float(r.latency_ms) for r in results]),
        "avgRetrievalMs": metrics.mean([float(r.retrieval_ms) for r in results]),
    }

    costs = [r.cost for r in results if r.cost is not None]
    summary["cost"] = sum(costs) if costs else None
    summary["currency"] = next((r.currency for r in results if r.currency), None)

    # 按探针类型拆开看：BM25 应该主要提升 lexical，重排主要提升 ndcg
    by_probe: dict[str, list[float]] = {}
    for result in answerable:
        by_probe.setdefault(result.case.probe, []).append(
            result.retrieval[f"recall@{top_k}"]
        )
    summary["recallByProbe"] = {
        probe: metrics.mean(values) for probe, values in sorted(by_probe.items())
    }
    return summary


async def run_variant(
    variant: Variant, cases: list[QuestionCase]
) -> tuple[dict[str, Any], list[QuestionResult]]:
    """套用变体配置跑完一轮，结束后恢复原配置。"""
    original = {key: getattr(settings, key) for key in variant.overrides}
    for key, value in variant.overrides.items():
        setattr(settings, key, value)

    try:
        adapter = OpenAICompatibleAdapter()
        knowledge = KnowledgeService()
        judge = AnswerJudge(adapter)

        chunks, reindexed = await ensure_corpus(knowledge)
        logger.info(
            "[%s] corpus ready: %s chunks%s",
            variant.name,
            chunks,
            " (reindexed)" if reindexed else "",
        )

        top_k = int(variant.overrides.get("RAG_TOP_K", settings.RAG_TOP_K))
        results: list[QuestionResult] = []
        for index, case in enumerate(cases, start=1):
            result = await _run_case(knowledge, judge, adapter, case, top_k)
            results.append(result)
            logger.info(
                "[%s] %s/%s %s recall=%.2f",
                variant.name,
                index,
                len(cases),
                case.id,
                result.retrieval[f"recall@{top_k}"],
            )
        summary = summarize(variant, results)
        summary["corpusChunks"] = chunks
        return summary, results
    finally:
        for key, value in original.items():
            setattr(settings, key, value)


async def run(
    variants: list[Variant], cases: list[QuestionCase]
) -> dict[str, Any]:
    # 评估依赖埋点来算成本与延迟，强制打开
    settings.TELEMETRY_ENABLED = True

    summaries: list[dict[str, Any]] = []
    details: dict[str, list[dict[str, Any]]] = {}
    for variant in variants:
        summary, results = await run_variant(variant, cases)
        summaries.append(summary)
        details[variant.name] = [
            {
                "id": result.case.id,
                "probe": result.case.probe,
                "question": result.case.question,
                "expected": result.case.expected_documents,
                "retrieved": result.retrieved_documents,
                "retrieval": result.retrieval,
                "keywordCoverage": result.keyword_coverage,
                "faithfulness": result.verdict.faithfulness,
                "relevance": result.verdict.relevance,
                "abstained": result.verdict.abstained,
                "judgeReason": result.verdict.reason,
                "judgeFailed": result.verdict.failed,
                "answer": result.answer,
                "latencyMs": result.latency_ms,
            }
            for result in results
        ]
    return {"summaries": summaries, "details": details}

