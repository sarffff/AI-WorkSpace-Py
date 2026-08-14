"""配置变体定义。

每个变体只相对 baseline 改动一到两个开关——一次改一堆参数，跑出差异也说不清
是谁的功劳。想看组合效应就显式定义组合变体（如 hybrid+rerank），而不是
指望从一堆混合结果里反推。

分块相关的开关（``CHUNK_*`` / ``EMBEDDING_MODEL``）会改变库里的分块与向量，
需要重建索引。这件事由 ``runner.ensure_corpus`` 通过分块指纹自动处理，
变体这边不需要额外声明。

每个变体都把关键开关写全（包括和 baseline 相同的值），这样一次运行的配置
完全由代码决定，不受本地 .env 影响——否则换台机器跑出的数字没法比。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    description: str
    overrides: dict[str, Any] = field(default_factory=dict)


# 所有变体共享的基线：关掉花钱的选项，只留免费的
_BASE = {
    "RAG_HYBRID": True,
    "RAG_MULTI_QUERY": False,
    "RAG_RERANK": False,
    "RAG_CONTEXT_WINDOW": 1,
    "RAG_TOP_K": 5,
    "CHUNK_MAX_TOKENS": 320,
    "GUARDRAIL_ENABLED": True,
    "GUARDRAIL_BLOCK_SCORE": 0,
    # 提示词也是一个可扫的维度。注意只有 eval_rag_answer 会被这套评估用到：
    # PROMPT_CHAT_SYSTEM_VERSION 管的是线上多轮 Agent 的系统提示词，本评估
    # 走的是单轮 RAG 问答（见模块顶部说明），把它写进变体只会得到一个
    # 「怎么改都没差别」的假结论。要对比那个，得先有一套 Agent 端到端评估。
    "PROMPT_EVAL_ANSWER_VERSION": "v1",
}

VARIANTS: dict[str, Variant] = {
    "baseline": Variant(
        name="baseline",
        description="混合检索 + 邻域扩展，不改写不重排（默认配置）",
        overrides=dict(_BASE),
    ),
    "dense-only": Variant(
        name="dense-only",
        description="关掉 BM25，只用向量检索——用来量化稀疏通道到底贡献了多少",
        overrides={**_BASE, "RAG_HYBRID": False},
    ),
    "no-context-window": Variant(
        name="no-context-window",
        description="关掉邻域扩展，验证补全被切断的上下文是否真的有用",
        overrides={**_BASE, "RAG_CONTEXT_WINDOW": 0},
    ),
    "rerank": Variant(
        name="rerank",
        description="baseline + LLM listwise 重排，每次检索多一次模型调用",
        overrides={**_BASE, "RAG_RERANK": True},
    ),
    "multi-query": Variant(
        name="multi-query",
        description="baseline + 多查询改写，每次检索多一次模型调用",
        overrides={**_BASE, "RAG_MULTI_QUERY": True},
    ),
    "rerank+multi-query": Variant(
        name="rerank+multi-query",
        description="改写与重排同时开启，看两者叠加是否还有增量",
        overrides={**_BASE, "RAG_RERANK": True, "RAG_MULTI_QUERY": True},
    ),
    "chunk-small": Variant(
        name="chunk-small",
        description="更小的分块（160 token），精度更高但更容易切断上下文",
        overrides={**_BASE, "CHUNK_MAX_TOKENS": 160},
    ),
    "chunk-large": Variant(
        name="chunk-large",
        description="更大的分块（640 token），上下文更完整但检索更钝",
        overrides={**_BASE, "CHUNK_MAX_TOKENS": 640},
    ),
    "top-k-3": Variant(
        name="top-k-3",
        description="只取 3 条参考内容，省 token，代价是召回下降",
        overrides={**_BASE, "RAG_TOP_K": 3},
    ),
    "no-guardrail": Variant(
        name="no-guardrail",
        description=(
            "关掉提示注入护栏——和 baseline 对照才能知道抗注入率里有多少是护栏的功劳，"
            "多少只是提示词在起作用"
        ),
        overrides={**_BASE, "GUARDRAIL_ENABLED": False},
    ),
    "guardrail-blocking": Variant(
        name="guardrail-blocking",
        description=(
            "把可疑资料整段拒绝注入（阈值 5）。抗注入率应当最高，"
            "同时要盯住其它探针的召回有没有因为误报而掉下来"
        ),
        overrides={**_BASE, "GUARDRAIL_BLOCK_SCORE": 5},
    ),
    "prompt-strict": Variant(
        name="prompt-strict",
        description=(
            "只换回答提示词（eval_rag_answer v2-strict）：规定「先结论后依据」并"
            "固定拒答句式。检索完全没动，所以检索指标应当逐位相同——"
            "如果召回也变了，那是哪里串了配置，不是提示词的功劳"
        ),
        overrides={**_BASE, "PROMPT_EVAL_ANSWER_VERSION": "v2-strict"},
    ),
}


def resolve(names: list[str] | None) -> list[Variant]:
    if not names:
        return [VARIANTS["baseline"]]
    if len(names) == 1 and names[0] == "all":
        return list(VARIANTS.values())
    unknown = [name for name in names if name not in VARIANTS]
    if unknown:
        raise SystemExit(
            f"未知变体: {', '.join(unknown)}\n可用: {', '.join(VARIANTS)} 或 all"
        )
    return [VARIANTS[name] for name in names]
