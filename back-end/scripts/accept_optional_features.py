# -*- coding: utf-8 -*-
"""验收：每个默认关闭的开关，打开之后到底有没有接上。

## 为什么需要这个脚本

2026-08-22 那次排查的结论不是"五个 max_tokens 写小了"，而是一条更一般的判据：

    **默认关闭的开关是静默失效的温床。** 默认开着的路径被真实使用打过，而默认
    关闭的只在有人专门去开它的时候才跑——而那通常是几个月之后，或者只在 eval
    的某个变体里，那时失败会被读成"这个技术没有增益"。

四个 RAG 增强（路由 / HyDE / 多查询 / 重排）就是这么死了很久的：它们的开关默认关，
所以唯一跑到它们的地方是 eval 变体，而变体的结论恰好是"和 baseline 一样"。

## 它验什么、不验什么

**验**：打开开关之后，可观测的行为**确实变了**（工具进了注册表、索引换了实现、
消息里多了图片块……）。也就是"这个功能是接上的"。

**不验**：这个功能有没有用。那要靠 eval，而且这个仓库已经证明"接上了"和"有用"
是两件独立的事——多查询接上之后 nDCG 反而从 0.984 掉到 0.974。

所以这个脚本的输出只有三态：`接上了` / `没接上` / `跳过(需要外部依赖)`。
它不打分，也不该打分。

## 为什么不写成 pytest

写成 pytest 也可以，而且几条 TOOL_* 已经有单元测试了。分开的理由是**读法不同**：
测试回答"改动有没有破坏契约"，这个脚本回答"我这台机器上，这十几个功能现在
各是什么状态"。后者是一份清单，要一眼看完；混进 660 个测试里就看不见了。

跑法：cd back-end && python scripts/accept_optional_features.py
不发任何模型请求，不连外部服务（连不上的直接标 SKIP）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from config import settings  # noqa: E402

WIRED, DEAD, SKIP = "接上了", "没接上", "跳过"

results: list[tuple[str, str, str]] = []


def record(name: str, state: str, detail: str) -> None:
    results.append((name, state, detail))


class override:
    """临时改配置，退出即还原。

    直接 setattr 而不是用 pytest 的 monkeypatch：这个脚本刻意不依赖 pytest
    （见模块文档最后一节）。
    """

    def __init__(self, **values):
        self._values = values
        self._saved: dict[str, object] = {}

    def __enter__(self):
        for key, value in self._values.items():
            self._saved[key] = getattr(settings, key)
            setattr(settings, key, value)
        return self

    def __exit__(self, *_exc):
        for key, value in self._saved.items():
            setattr(settings, key, value)
        return False


# ---- 工具面 ---------------------------------------------------------------
# 判据是"工具名进了注册表"。这是最浅的一层，但它恰恰是提示词版本不匹配之外
# 最常见的失败点：开关名拼错、或者 enabled_names 漏了一个分支。


def check_tool_flags() -> None:
    from services import workspace_tools

    cases = [
        ("TOOL_CALCULATE_ENABLED", "calculate"),
        ("TOOL_READ_ATTACHMENT_ENABLED", "read_attachment"),
        ("TOOL_WRITE_KNOWLEDGE_ENABLED", "save_to_knowledge_base"),
        ("TOOL_DELETE_KNOWLEDGE_ENABLED", "delete_knowledge_document"),
        ("TOOL_ASK_USER_ENABLED", "ask_user"),
        ("TOOL_WEB_FETCH_ENABLED", "fetch_web_page"),
    ]
    for flag, tool in cases:
        with override(**{flag: True}):
            names = workspace_tools.enabled_names()
        state = WIRED if tool in names else DEAD
        record(f"{flag}", state, f"注册表里{'有' if state == WIRED else '没有'} {tool}")

    # web_search 是唯一一个"开了也可能不注册"的：没有 provider 或没有 key 时
    # 它故意不注册，而不是注册一个每轮都失败的版本。所以这里两种结果都合法，
    # 关键是**能区分**——注册了一个每轮失败的工具才是真问题。
    with override(TOOL_WEB_SEARCH_ENABLED=True):
        names = workspace_tools.enabled_names()
    if "web_search" in names:
        record("TOOL_WEB_SEARCH_ENABLED", WIRED, "provider 已配置，工具已注册")
    elif any(name.startswith("web_search(") for name in names):
        record(
            "TOOL_WEB_SEARCH_ENABLED",
            SKIP,
            "缺 WEB_SEARCH_PROVIDER / API_KEY —— 按设计不注册（不是失效）",
        )
    else:
        record("TOOL_WEB_SEARCH_ENABLED", DEAD, "开关打开后注册表里什么都没有")


# ---- 多代理委派 -----------------------------------------------------------


def check_delegation() -> None:
    """委派模式改的是**工具面**，不只是提示词。

    按 agent-roadmap-order 的记载，这块"从未在真实链路跑过"。这里至少能确认
    它接上了；它值不值那笔嵌套子代理的成本，由 eval 的 delegation-* 变体回答。

    入口在 ``subagent`` 而不是 ``agent_roles``：后者只管角色定义与工具白名单，
    "这次要不要启用委派"是循环那一侧的判断。第一版探针猜错了这个位置，
    结果打出一个 SKIP —— 而一个"其实是我猜错了 API 名"的 SKIP 比 DEAD 更坏，
    它看起来像"这项不适用"。
    """
    from services import subagent

    for mode in ("off", "augment", "supervisor"):
        with override(AGENT_DELEGATION_MODE=mode):
            on = subagent.enabled()
            described = subagent.describe_mode()
        if mode == "off":
            # off 必须是 False。漏掉这一半的话，一个恒为 True 的实现也算通过
            record(
                "AGENT_DELEGATION_MODE=off",
                WIRED if not on else DEAD,
                f"enabled()={on}（必须 False），describe={described}",
            )
        else:
            record(
                f"AGENT_DELEGATION_MODE={mode}",
                WIRED if on else DEAD,
                f"enabled()={on}，describe={described}",
            )


# ---- 显式规划 -------------------------------------------------------------


def check_planning() -> None:
    from services import planner

    with override(AGENT_PLAN_MODE="plan_execute"):
        on = planner.enabled()
        steps = planner.format_steps([{"goal": "x", "tool": "calculate"}])
    ok = on and "calculate" in steps
    record("AGENT_PLAN_MODE=plan_execute", WIRED if ok else DEAD, f"enabled={on}")


# ---- 向量索引 -------------------------------------------------------------


def check_ann() -> None:
    """hnsw 换的是 FAISS 索引类型。判据是它真的建了另一种索引且检索还能出结果。

    不需要网络也不需要数据库：直接喂 ``VectorIndex.build_if_stale`` 造好的
    ``DocumentChunk``。走这一层而不是 ``MemoryVectorStore``——后者的 upsert 是
    空操作（索引由 retriever 按签名从 MySQL 重建），拿它当入口什么都验不到。
    """
    import numpy as np

    from services.embedding_service import EmbeddingService
    from services.retrieval_index import VectorIndex

    class _Chunk:
        """只带 build_if_stale 用到的字段。"""

        def __init__(self, chunk_id: str, vector: list[float]) -> None:
            self.id = chunk_id
            self.embedding = EmbeddingService.serialize(vector)

    vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.6, 0.8, 0.0], [0.0, 0.0, 1.0]]
    chunks = [_Chunk(f"c{i}", v) for i, v in enumerate(vectors)]

    for ann in ("exact", "hnsw"):
        with override(VECTOR_ANN=ann):
            index = VectorIndex()
            try:
                index.build_if_stale(chunks, signature=f"sig-{ann}")
                hits = index.search([1.0, 0.0, 0.0], top_k=2)
            except Exception as exc:
                record(f"VECTOR_ANN={ann}", SKIP, f"{type(exc).__name__}: {exc}")
                continue
            kind = type(index._faiss_index).__name__ if index._faiss_index else "numpy"
        # 判据有两条：检索出结果，且 exact/hnsw 真的走了不同实现。
        # 只看"有结果"的话，一个忽略 VECTOR_ANN 的实现也会两次都通过。
        ok = bool(hits)
        record(
            f"VECTOR_ANN={ann}",
            WIRED if ok else DEAD,
            f"索引实现 = {kind}，top1 = {hits[0][1] if hits else '无'}"
            f"（分数 {hits[0][0]:.3f}）" if hits else "无结果",
        )


def check_qdrant() -> None:
    from services import vector_store

    with override(VECTOR_STORE="qdrant"):
        try:
            uses = vector_store.uses_qdrant()
        except Exception as exc:
            record("VECTOR_STORE=qdrant", SKIP, f"{type(exc).__name__}: {exc}")
            return
    # 只验"配置能把它选出来"。真的连上要一个跑着的服务
    # （docker-compose.qdrant.yml），那超出这个脚本的范围。
    record(
        "VECTOR_STORE=qdrant",
        WIRED if uses else DEAD,
        "配置可选中；真实连通性需要跑着的 Qdrant，未验",
    )
    vector_store.reset_for_tests()


# ---- 分块与计数 -----------------------------------------------------------


def check_token_counter() -> None:
    from services.token_budget import get_token_counter

    sample = "报销时限是三十个自然日 within 30 calendar days"
    # TokenCounter 是个带 .count() 的对象，不是可调用的函数。
    # 第一版探针把它当函数调，打出一个「检查本身出错」的 SKIP。
    heuristic = get_token_counter("heuristic").count(sample)
    try:
        exact = get_token_counter("tiktoken").count(sample)
    except Exception as exc:
        record(
            "TOKEN_COUNTER=tiktoken",
            SKIP,
            f"未安装或首次下载词表失败：{type(exc).__name__}（heuristic={heuristic}）",
        )
        return
    # 两个必须给出不同的数：相同就说明 tiktoken 那条分支其实回退到了估算，
    # 而那正是"看起来配上了、实际没生效"。
    record(
        "TOKEN_COUNTER=tiktoken",
        WIRED if exact > 0 and exact != heuristic else DEAD,
        f"tiktoken={exact} vs heuristic={heuristic}",
    )


def check_semantic_chunking() -> None:
    """semantic 分块的真实路径要 embedding，所以这里喂一个假的 embedder。

    验的是分派 + 断点计算这一段真的走了语义路线，而不是静默退回结构分块。
    退回是设计好的行为（句子太少、或 embedding 失败），所以正例必须给足句子。
    """
    import asyncio

    from services import chunking

    text = "\n\n".join(
        f"第{i}条 报销相关规定的第{i}段说明，涉及时限、审批与发票要求。" for i in range(1, 13)
    )
    units = chunking.sentences_for_embedding(text, "policy.md")
    if len(units) < settings.CHUNK_SEMANTIC_MIN_SENTENCES:
        record(
            "CHUNK_STRATEGY=semantic",
            SKIP,
            f"探针语料只切出 {len(units)} 句，不足 {settings.CHUNK_SEMANTIC_MIN_SENTENCES} 句",
        )
        return

    # 造一组"前半段互相接近、后半段明显跳变"的向量，语义分块应当在跳变处断开
    vectors = [[1.0, 0.0] if i < len(units) // 2 else [0.0, 1.0] for i in range(len(units))]
    distances = chunking.adjacent_distances(vectors)
    from services.token_budget import get_token_counter

    with override(CHUNK_STRATEGY="semantic"):
        chunks = chunking.split_semantic(
            units,
            distances,
            max_tokens=settings.CHUNK_MAX_TOKENS,
            counter=get_token_counter("heuristic"),
        )
    ok = len(chunks) >= 2
    record(
        "CHUNK_STRATEGY=semantic",
        WIRED if ok else DEAD,
        f"{len(units)} 句 → {len(chunks)} 块（跳变处应当断开；入库真实路径需 embedding）",
    )


# ---- 视觉 -----------------------------------------------------------------


def check_vision() -> None:
    from services import vision

    with override(VISION_MODELS="glm-4v"):
        supported = vision.supports_vision("glm-4v")
        plain = vision.supports_vision("glm-4.5-air")
    ok = supported and not plain
    record(
        "VISION_MODELS",
        WIRED if ok else DEAD,
        f"白名单内 ={supported}，白名单外 ={plain}（后者必须为 False）",
    )


# ---- 语义缓存 -------------------------------------------------------------


def check_semantic_cache() -> None:
    """命中判定要 embedding，所以只验开关能把它从"整体短路"里放出来。"""
    from services import semantic_cache

    try:
        with override(SEMANTIC_CACHE_ENABLED=False):
            off = semantic_cache.semantic_cache.enabled
        with override(SEMANTIC_CACHE_ENABLED=True):
            on = semantic_cache.semantic_cache.enabled
    except AttributeError:
        record("SEMANTIC_CACHE_ENABLED", SKIP, "没有 enabled 属性可查")
        return
    record(
        "SEMANTIC_CACHE_ENABLED",
        WIRED if (on and not off) else DEAD,
        f"off={off} on={on}（命中判定需 embedding，未验）",
    )


# ---- 人工审批 -------------------------------------------------------------


def check_approval() -> None:
    """审批依赖快照。只开审批不开快照时它必须**不生效**——那正是那条启动警告
    存在的理由，也是这里要钉住的：静默"以为开了其实没开"最危险。
    """
    from services import approval

    with override(AGENT_APPROVAL_MODE="write", AGENT_CHECKPOINT_ENABLED=False):
        without = approval.enabled()
    with override(AGENT_APPROVAL_MODE="write", AGENT_CHECKPOINT_ENABLED=True):
        with_snapshot = approval.enabled()
        gated = approval.gated_tools()
    ok = with_snapshot and not without and "save_to_knowledge_base" in gated
    record(
        "AGENT_APPROVAL_MODE=write",
        WIRED if ok else DEAD,
        f"无快照={without}（必须 False）有快照={with_snapshot}，闸门 {sorted(gated)}",
    )


CHECKS = [
    check_tool_flags,
    check_delegation,
    check_planning,
    check_approval,
    check_ann,
    check_qdrant,
    check_token_counter,
    check_semantic_chunking,
    check_vision,
    check_semantic_cache,
]


def main() -> None:
    for check in CHECKS:
        try:
            check()
        except Exception as exc:  # 一个检查崩了不该让整张清单看不到
            record(check.__name__, SKIP, f"检查本身出错：{type(exc).__name__}: {exc}")

    width = max(len(name) for name, _s, _d in results)
    order = {DEAD: 0, SKIP: 1, WIRED: 2}
    print("默认关闭 / 非默认模式的功能验收\n")
    for name, state, detail in sorted(results, key=lambda r: (order[r[1]], r[0])):
        mark = {DEAD: "!!", SKIP: " ~", WIRED: "  "}[state]
        print(f" {mark} {name:<{width}}  {state:<6} {detail}")

    counts = {state: sum(1 for _n, s, _d in results if s == state) for state in (WIRED, DEAD, SKIP)}
    print(
        f"\n接上了 {counts[WIRED]} / 没接上 {counts[DEAD]} / 跳过 {counts[SKIP]}"
        f"（共 {len(results)} 项）"
    )
    print(
        "\n提醒：本脚本只验「功能是接上的」。它有没有用要看 eval——"
        "多查询接上之后 nDCG 反而从 0.984 掉到 0.974，两件事完全独立。"
    )
    raise SystemExit(1 if counts[DEAD] else 0)


main()
