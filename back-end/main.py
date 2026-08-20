import os

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware

from rate_limit import limiter

from config import settings
from database import init_db, SessionLocal
from models import Prompt
from routers import (
    chat_router,
    knowledge_router,
    auth_router,
    prompt_router,
    settings_router,
    attachment_router,
    metrics_router,
    feedback_router,
    memory_router,
    workspace_router,
)
from services import approval
from services import prompt_library
from services import ingest_clean
from services import retriever
from services import subagent
from services import vector_store
from services import workspace_tools
from services.rerank import rerank_client

app = FastAPI(
    title="AI Workspace API",
    description="AI 助手桌面应用的后端 API",
    version="1.0.0",
    # 生产环境关闭文档接口
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ========== 安全响应头中间件 ==========
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# CORS 白名单
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(auth_router.router)
app.include_router(chat_router.router)
app.include_router(knowledge_router.router)
app.include_router(prompt_router.router)
app.include_router(settings_router.router)
app.include_router(attachment_router.router)
app.include_router(metrics_router.router)
app.include_router(feedback_router.router)
app.include_router(memory_router.router)
app.include_router(workspace_router.router)

# 静态文件服务：附件上传后的访问入口
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")


@app.get("/")
async def root():
    """API 根路径"""
    return {
        "message": "AI Workspace API",
        "version": "1.0.0",
        "status": "running"
    }


def _seed_prompts():
    """首次启动时写入内置提示词模板（若表中无数据）"""
    db = SessionLocal()
    try:
        if db.query(Prompt).count() == 0:
            seeds = [
                Prompt(
                    title="代码重构专家",
                    description="审查代码的性能、可读性与 TypeScript 最佳实践，并给出具体重构建议。",
                    category="Engineering",
                    content="你是一名资深的代码重构专家，精通 TypeScript/React/Python。请审查以下代码，从性能、可读性、类型安全、可维护性四个维度给出改进建议，并给出重构后的代码示例。\n\n用户输入：{input}",
                    is_public=True,
                ),
                Prompt(
                    title="架构设计规划师",
                    description="设计可扩展的 monorepo 工作区与后端架构。",
                    category="Architecture",
                    content="你是一名系统架构师，擅长 monorepo（pnpm workspaces / Turborepo）与 NestJS/FastAPI 后端。请基于以下需求，给出模块划分、依赖关系、目录结构与关键接口设计。\n\n需求：{input}",
                    is_public=True,
                ),
                Prompt(
                    title="SQL 查询优化器",
                    description="优化慢 SQL 查询并设计高效的 Prisma / SQLAlchemy 关系。",
                    category="Database",
                    content="你是数据库性能优化专家。请分析以下 SQL/ORM 查询，指出索引、连接、N+1 等问题，给出优化后的查询和必要的索引建议。\n\n查询：{input}",
                    is_public=True,
                ),
                Prompt(
                    title="技术文档撰写助手",
                    description="把零散笔记整理为结构清晰的 API / 产品文档。",
                    category="Writing",
                    content="你是一名技术文档工程师。请把以下零散笔记整理成结构清晰的文档，包含概述、接口表、参数说明和示例。\n\n笔记：{input}",
                    is_public=True,
                ),
            ]
            db.add_all(seeds)
            db.commit()
            print(f"Seeded {len(seeds)} built-in prompts.")
    finally:
        db.close()


def _check_prompt_matches_config() -> None:
    """校验系统提示词版本与运行时配置是否匹配。

    此前这件事只写在模板的 notes 里（"开启委派时必须切到这一版"）和一条启动
    警告里。两者都拦不住任何人：配置和提示词不一致时程序照跑，表现是模型能从
    schema 看到 delegate、却不知道任务描述必须自包含，于是把简单问题也派出去，
    看起来像"多代理没用"——一个配置错误伪装成了功能缺陷。

    委派错配直接拒绝启动，因为它不是"次优"而是"错"：supervisor 模式下用
    augment 的提示词，主代理会按"我自己能查知识库"去规划，然后发现没有那个
    工具。workspace 工具错配只警告——那时工具本身可用，模型只是少了几条策略，
    而且 eval 的 prompt-v2 变体故意要跑这个组合来量化 v4 的价值。
    """
    # 走 resolve_version 而不是直接读 settings。取到的模板本来就是对的（get()
    # 内部也会 resolve，空串会照样落到契约默认版本），问题在下面那几条报错和
    # 警告里的 {version}：配置项留空时——现在的默认状态——它们会印成
    # "PROMPT_CHAT_SYSTEM_VERSION=" 后面什么都没有，而这几条消息的全部作用
    # 就是告诉人"你现在用的是哪一版、该换成哪一版"。
    version = prompt_library.resolve_version("chat_system_rag")
    template = prompt_library.get("chat_system_rag", version)
    mode = settings.AGENT_DELEGATION_MODE

    if mode in ("augment", "supervisor"):
        required = ("delegation", "supervisor") if mode == "supervisor" else ("delegation",)
        if not template.expects_all(*required):
            raise RuntimeError(
                f"AGENT_DELEGATION_MODE={mode} 与 PROMPT_CHAT_SYSTEM_VERSION="
                f"{version} 不匹配：该版本没有声明 expects: {', '.join(required)}，"
                "也就是没有讲这种模式下该怎么委派。请改用 "
                f"{'v6-supervisor' if mode == 'supervisor' else 'v5-augment'}，"
                "或把 AGENT_DELEGATION_MODE 设回 off。"
            )
        # supervisor 的提示词讲的是"你没有检索工具，必须委派"，在 augment 模式下
        # 这是假的——那时主代理手上有全部工具，照它规划会白绕一圈。
        if mode == "augment" and "supervisor" in template.expects:
            raise RuntimeError(
                f"AGENT_DELEGATION_MODE=augment 但 PROMPT_CHAT_SYSTEM_VERSION="
                f"{version} 是为 supervisor 模式写的（它告诉模型自己没有检索、"
                "计算等工具，而 augment 模式下主代理保留全部工具）。"
                "请改用 v5-augment，或把模式设为 supervisor。"
            )

    elif "delegation" in template.expects:
        # 反向错配：提示词讲的是怎么委派，但 delegate 根本没注册。模型会按
        # "我可以派人"去规划；v6 更进一步，它还告诉模型自己没有检索工具——
        # 那在单代理模式下是假的。提示词承诺了一个不存在的工具，同一类错误。
        raise RuntimeError(
            f"PROMPT_CHAT_SYSTEM_VERSION={version} 是为委派模式写的"
            f"（expects: {', '.join(template.expects)}），但 AGENT_DELEGATION_MODE=off"
            " 时 delegate 不会注册。请把模式设为 augment/supervisor，"
            "或改用不讲委派的版本（v2 / v3-lean / v4-workspace）。"
        )

    if workspace_tools.enabled_names() and "workspace-tools" not in template.expects:
        print(
            f"  警告：已启用 workspace 工具，但 chat_system_rag/{version} "
            "没有讲它们的使用策略（expects 里没有 workspace-tools）。"
            "模型只能从 schema 看到工具名，建议切到 v4-workspace 或更高版本。"
        )


def _check_ingest_backend() -> None:
    """校验 PDF 结构恢复的依赖装上了。

    这一条必须**拒绝启动**，不能只警告：缺了 pdfplumber 时每一份 PDF 都会静默退回
    无结构抽取，于是 ``heading_path`` 恒为空——``chunking`` 承诺的「标题路径」与
    「章节边界优先」两件事全部失效，而文档状态照样是 ``indexed``。更糟的是同一份
    PDF 在两台机器上会切出不同的块，而 eval 的结论就依赖这个。

    这正是把它定成必需依赖而不是可选降级的理由；只做成一条警告等于把那个决定
    又变回可选。
    """
    if not settings.INGEST_PDF_STRUCTURE:
        return
    if not ingest_clean.structure_backend_available():
        raise RuntimeError(
            "INGEST_PDF_STRUCTURE=true 但 pdfplumber 没有安装。"
            "PDF 会静默退回无结构抽取（标题层级丢失、词内空格不修），"
            "而文档状态仍是 indexed——这种失败查不出来。"
            "请 pip install -r requirements.txt，或把 INGEST_PDF_STRUCTURE 设为 false。"
        )


@app.on_event("startup")
async def startup():
    """应用启动时执行"""
    init_db()
    _seed_prompts()
    # 提示词模板有问题（占位符对不上、条件段没闭合、默认版本已归档）就在这里
    # 起不来，而不是等第一个用户提问时才在 500 里暴露。
    prompt_library.validate()
    _check_ingest_backend()
    # 工具是按开关注册的，而"开关开了但没配 key 的 web_search 根本不注册"这类
    # 静默行为在界面上只表现为"模型不用那个工具"——分不清是没注册还是模型不想用。
    # 所以启动时把实际注册了哪些打出来，这是排查工具类问题的第一现场。
    print(f"Workspace tools: {', '.join(workspace_tools.enabled_names()) or '(全部关闭)'}")
    print(f"Vision models: {settings.VISION_MODELS or '(未启用，图片只会以链接形式留在提示词里)'}")
    # 同样走 resolve_version：这一行是排查提示词问题的第一现场，打出来的必须是
    # 真正生效的版本，而不是配置项的字面值（留空时那是空串）。
    print(
        "Chat system prompt: chat_system_rag/"
        f"{prompt_library.resolve_version('chat_system_rag')}"
    )
    print(f"Agent delegation: {subagent.describe_mode()}")
    print(f"Agent approval: {approval.describe_mode()}")
    # 审批模式开着但快照关着 = 审批静默失效。两个开关分开是对的(快照本身有用,
    # 重放和 agent_runs 都靠它),但这个组合永远是配置错误,不该只能靠"点了同意
    # 没反应"发现。
    if settings.AGENT_APPROVAL_MODE != "off" and not settings.AGENT_CHECKPOINT_ENABLED:
        print(
            "  ⚠ AGENT_APPROVAL_MODE 已开启，但 AGENT_CHECKPOINT_ENABLED=false —— "
            "审批要跨请求恢复，没有快照就无从恢复，因此当前不会生效。"
        )
    # 这三个都会改变循环行为或提示词正文,而它们的效果在界面上都看不见:
    # 缓存命中只体现在账单上,重复拦截只体现在少跑一次工具。启动时打出来,
    # 排查"为什么这次和上次不一样"时不用去翻 .env。
    print(
        "Prompt cache: stable prefix "
        + ("on" if settings.PROMPT_CACHE_STABLE_PREFIX else "off（对照组）")
    )
    print(
        "Repeat guard: "
        + (
            f"同一 (工具, 参数) 上限 {settings.AGENT_REPEAT_LIMIT} 次"
            if settings.AGENT_REPEAT_LIMIT > 0
            else "off（不检测重复调用）"
        )
    )
    print(f"Structured output retries: {settings.STRUCTURED_OUTPUT_RETRIES}")
    # 摄取与检索这几项同样"改变行为但界面上看不见":清洗关掉只表现为某些文档
    # 检索不到,重排换后端只表现为顺序不同,向量库降级更是完全无声。
    print(
        "Ingest: clean="
        + ("on" if settings.INGEST_CLEAN else "off（对照组）")
        + f", pdf={'pdfplumber' if settings.INGEST_PDF_STRUCTURE else 'pypdf2'}"
        + f", 编码先验={settings.INGEST_ENCODING_HINTS or '(无)'}"
        + f", 自检={'on' if settings.INGEST_SELF_CHECK else 'off'}"
    )
    print(f"Chunking: {settings.CHUNK_STRATEGY} (max {settings.CHUNK_MAX_TOKENS} tokens)")
    _print_rerank_mode()
    _print_vector_store()
    if settings.RAG_HYDE or settings.RAG_QUERY_ROUTE:
        print(
            "Query rewriting: "
            + ", ".join(
                filter(
                    None,
                    [
                        "HyDE（假答案只喂稠密通道）" if settings.RAG_HYDE else "",
                        "路由（按意图调 RRF 权重）" if settings.RAG_QUERY_ROUTE else "",
                    ],
                )
            )
        )
    # 提示词与配置的匹配校验。必须在 prompt_library.validate() 之后——它要按
    # 版本名取模板，版本不存在时应当由 validate 那边给出更清楚的报错。
    _check_prompt_matches_config()
    print(f"AI Workspace Server (Python) is running on: http://localhost:{settings.PORT}")


def _print_rerank_mode() -> None:
    mode = retriever.rerank_mode()
    if mode == "off":
        print("Rerank: off")
        return
    if mode == "api" and not rerank_client.configured:
        # 不静默退回 llm：那会让"api 比 llm 好多少"这个对比测的是同一个东西
        print(
            f"  警告：RAG_RERANK_MODE=api 但 rerank 接口未配置"
            f"（endpoint={rerank_client.endpoint}），重排会退回融合序、等于没开。"
        )
        return
    detail = (
        f"cross-encoder {settings.RERANK_MODEL} @ {rerank_client.endpoint}"
        if mode == "api"
        else f"LLM listwise（{settings.utility_model}，对照组）"
    )
    print(f"Rerank: {mode} — {detail}，候选 {settings.RAG_RERANK_CANDIDATES}")


def _print_vector_store() -> None:
    """打印**实际**生效的后端，而不是配置里写的那个。

    这两件事会不一致：配置写着 qdrant 但服务连不上时已经降级回 memory。而降级
    是完全无声的——检索照样有结果（走的是进程内索引），只是"多 worker 共享、
    重启不丢"这些收益一个都没有。不打出来的话没人会发现。
    """
    configured = (settings.VECTOR_STORE or "memory").strip().lower()
    actual = "qdrant" if vector_store.uses_qdrant() else "memory"
    if actual == "qdrant":
        print(
            f"Vector store: qdrant @ {settings.QDRANT_URL}"
            f" (collection={settings.QDRANT_COLLECTION},"
            f" M={settings.VECTOR_HNSW_M}, efC={settings.VECTOR_HNSW_EF_CONSTRUCT},"
            f" efS={settings.VECTOR_HNSW_EF_SEARCH})"
        )
        return
    ann = (settings.VECTOR_ANN or "exact").strip().lower()
    suffix = (
        f"HNSW(M={settings.VECTOR_HNSW_M}, efS={settings.VECTOR_HNSW_EF_SEARCH})"
        if ann == "hnsw"
        else "精确检索"
    )
    print(f"Vector store: memory — {suffix}，多 worker 部署时每个 worker 各建一份")
    if configured == "qdrant":
        print(
            "  警告：VECTOR_STORE=qdrant 但已降级回进程内索引。"
            "先 docker compose -f docker-compose.qdrant.yml up -d，"
            "再 python scripts/backfill_qdrant.py。"
        )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=settings.PORT)
