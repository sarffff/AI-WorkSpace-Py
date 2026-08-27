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
    # 语料降级与清洗:baseline 是"干净语料 + 清洗开着"。
    # 两个都写全,否则 dirty-* 变体跑完不恢复,后面所有变体测的都是脏语料。
    "EVAL_CORPUS_DEGRADE": "none",
    "INGEST_CLEAN": True,
    "INGEST_PDF_STRUCTURE": True,
    # 检索侧的新开关也全写上。RAG_RERANK_MODE 留空表示"按 RAG_RERANK 布尔量决定",
    # 于是既有的 rerank 变体不用改一个字。
    "RAG_RERANK_MODE": "",
    "RAG_HYDE": False,
    "RAG_QUERY_ROUTE": False,
    "CHUNK_STRATEGY": "structural",
    "VECTOR_STORE": "memory",
    "VECTOR_ANN": "exact",
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
    # ===== 检索质量 =====
    "rerank-api": Variant(
        name="rerank-api",
        description=(
            "专用 cross-encoder 重排（智谱 /rerank）。和 rerank（LLM listwise）"
            "配对读，差值就是「专用重排比让通用模型排序好多少」。"
            "nDCG 应当明显高于 rerank，而召回集合不变——重排只改顺序"
        ),
        overrides={**_BASE, "RAG_RERANK_MODE": "api"},
    ),
    "rerank-api+multi-query": Variant(
        name="rerank-api+multi-query",
        description=(
            "改写扩召回 + cross-encoder 精排。这是「先把召回做宽再把精度做窄」的"
            "标准组合，也是这套管线成本最高的配置"
        ),
        overrides={**_BASE, "RAG_RERANK_MODE": "api", "RAG_MULTI_QUERY": True},
    ),
    "hyde": Variant(
        name="hyde",
        description=(
            "HyDE：先编一段假答案，只拿它喂稠密通道（BM25 仍用原始 query）。"
            "预期提升集中在 paraphrase 探针上——问题与文档不在同一语域正是它治的病。"
            "反过来 lexical 探针不该掉，掉了说明假答案污染到了字面通道"
        ),
        overrides={**_BASE, "RAG_HYDE": True},
    ),
    "query-route": Variant(
        name="query-route",
        description=(
            "查询路由：判断偏字面还是偏语义，据此调 RRF 两路权重。"
            "trace 里的 route_intent 可以直接和数据集的 probe 标注对一遍，"
            "所以这个分类器的准确率是可测的，不用凭感觉"
        ),
        overrides={**_BASE, "RAG_QUERY_ROUTE": True},
    ),
    "chunk-semantic": Variant(
        name="chunk-semantic",
        description=(
            "语义分块：按相邻句向量距离找断点，而不是按空行。"
            "重点看 boundary / cross_section 两个探针——它们考的正是"
            "答案跨段落时分块有没有切在错的地方。入库时 embedding 调用量翻倍"
        ),
        overrides={**_BASE, "CHUNK_STRATEGY": "semantic"},
    ),
    # ===== 向量存储 =====
    # 这两个变体量的**不是**回答质量，是 ANN 与向量库的代价。当前语料几千个向量，
    # 精确检索召回本来就是 100%，所以正确的预期是「召回略降或不变、延迟变化」。
    # 如果 ann-hnsw 的召回明显下降，说明参数太激进（调 VECTOR_HNSW_EF_SEARCH），
    # 而不是"HNSW 不好用"。
    "ann-hnsw": Variant(
        name="ann-hnsw",
        description=(
            "进程内索引换成 HNSW 近似最近邻。和 baseline 比 recall@5 与 "
            "avgRetrievalMs：这是「ANN 拿召回换延迟」这笔交易在本项目规模下的实价。"
            "小库上它大概率是净亏——知道亏多少，比笼统说「大了要上 ANN」有用"
        ),
        overrides={**_BASE, "VECTOR_ANN": "hnsw"},
    ),
    "qdrant": Variant(
        name="qdrant",
        description=(
            "向量走 Qdrant。**需要先起服务并回填**（docker-compose.qdrant.yml + "
            "scripts/backfill_qdrant.py），否则会降级回 memory 后端跑出一份"
            "和 baseline 一样的数字——那不是「Qdrant 没差别」，是它根本没被用到。"
            "召回应当与 baseline 接近；真正的收益（多 worker 共享、重启不丢）"
            "这套单进程评估量不出来"
        ),
        overrides={**_BASE, "VECTOR_STORE": "qdrant"},
    ),
    # ===== 脏语料：两个不同的问题，别混着读 =====
    #
    # A) dirty-pdf-like（无 +clean 对照）——量的是「丢掉结构要付多少代价」。
    #    它的损伤清洗**修不了**：词内空格、丢掉的 # 标记、页眉页脚，全都只能靠
    #    PDF 的字号与坐标复原，而降级产物是纯文本，没有几何信息。所以配一个
    #    +clean 对照组只会得到"清洗毫无作用"这个假结论。和 baseline 比。
    #
    # B) dirty-gbk / dirty-unicode（成对）——量的是「清洗追回了多少」。
    #    这两类损伤清洗修得了，差值才有意义。
    "dirty-pdf-like": Variant(
        name="dirty-pdf-like",
        description=(
            "语料按 PyPDF2 抽取 PDF 的样子降级（抹掉 # 标记、注入页眉页码、"
            "长英文词里插空格、删空行）。和 baseline 比，差值就是「结构丢了值多少」，"
            "也就是第 1 条 PDF 结构恢复的动机。重点看 recallByProbe 的 lexical——"
            "RESOURCE_EXHAUSTED 被切成 RESOURC E_EXHAU STED 之后 BM25 的词元就没了"
        ),
        overrides={**_BASE, "EVAL_CORPUS_DEGRADE": "pdf_like"},
    ),
    "dirty-gbk": Variant(
        name="dirty-gbk",
        description=(
            "语料真的用 GBK 重新编码，编码嗅探关闭。走 errors=\"replace\" 之后正文"
            "变成一串 U+FFFD → tokenize 返回空 → BM25 建索引时整块跳过。"
            "预期召回接近 0，且入库自检把文档判成 failed 并写明原因"
        ),
        overrides={**_BASE, "EVAL_CORPUS_DEGRADE": "gbk_bytes", "INGEST_CLEAN": False},
    ),
    "dirty-gbk+clean": Variant(
        name="dirty-gbk+clean",
        description=(
            "同一份 GBK 语料，编码嗅探打开。这一对的差值应当接近「从完全不可用"
            "到和 baseline 齐平」——七条改造里差值最大、也最容易被忽略的一条"
        ),
        overrides={**_BASE, "EVAL_CORPUS_DEGRADE": "gbk_bytes", "INGEST_CLEAN": True},
    ),
    "dirty-unicode": Variant(
        name="dirty-unicode",
        description=(
            "全角 ASCII + 词内零宽字符 + CRLF + 控制字符，清洗关闭。"
            "这类损伤比 pdf_like 隐蔽得多：屏幕上看起来完全正常，但 ４２９ 在"
            "tokenize 眼里不是 429，夹了 U+200B 的词是两个词元"
        ),
        overrides={
            **_BASE,
            "EVAL_CORPUS_DEGRADE": "noisy_unicode",
            "INGEST_CLEAN": False,
        },
    ),
    "dirty-unicode+clean": Variant(
        name="dirty-unicode+clean",
        description=(
            "同一份语料，清洗打开。clean_text 折全角、去零宽、规整换行,"
            "所以这一对的差值全部归 clean_text —— 不掺 PDF 结构恢复那一侧"
        ),
        overrides={
            **_BASE,
            "EVAL_CORPUS_DEGRADE": "noisy_unicode",
            "INGEST_CLEAN": True,
        },
    ),
    # C) format-docx——既不是"脏"也不是"清洗"，量的是**我们的解析器保真吗**。
    #    和 dirty-pdf-like 正好相反：那个刻意丢结构，这个刻意保留结构。
    "format-docx": Variant(
        name="format-docx",
        description=(
            "语料转成真 .docx 上传（标题→Heading 样式、Markdown 表格→Word 表格），"
            "走 services/ingest_clean.extract_docx 读回来。md→docx→解析是一条近乎"
            "恒等的往返，所以**预期贴着 baseline**；掉下来就是解析器丢了真东西"
            "（标题层级没识别、表格被搬到文末、单元格换行没压平），而这些症状在"
            "单元测试里都表现为「能读出文字」所以全绿。"
            "重点看 recallByProbe 的 table_lookup：6 条金标里 5 条的答案在表格里"
        ),
        overrides={**_BASE, "EVAL_CORPUS_DEGRADE": "docx"},
    ),
    "dirty-scanned": Variant(
        name="dirty-scanned",
        description=(
            "图片型 PDF：有文件、抽不出任何文本。这个变体不是用来比召回的"
            "（必然是 0），它验证的是入库自检真的把文档判成了 failed —— "
            "改动前它会落成 status=indexed、chunks=0，界面上和正常文档毫无区别"
        ),
        overrides={**_BASE, "EVAL_CORPUS_DEGRADE": "scanned"},
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
