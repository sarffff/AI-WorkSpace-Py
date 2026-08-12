"""
应用配置模块
使用 pydantic-settings 自动从环境变量和 .env 文件读取配置
"""

import os
from typing import Optional

from pydantic_settings import BaseSettings


_DEFAULT_JWT_KEY = "your-secret-key-change-this-in-production"


class Settings(BaseSettings):
    """
    应用配置类
    """
    # ========== 数据库配置 ==========
    DATABASE_URL: str = "mysql+pymysql://root:password@localhost:3306/ai_workspace_py"

    # ========== 服务器配置 ==========
    PORT: int = 3000
    ENV: str = "dev"  # dev / production

    # ========== LLM API 配置 ==========
    LLM_API_KEY: str = "your_api_key_here"
    LLM_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4/"
    LLM_MODEL: str = "glm-4.5-air"
    # 流式请求附带 stream_options.include_usage 以拿到真实 token 用量。
    # 部分 OpenAI 兼容端点不认这个参数,被拒一次后会自动停发并改用本地估算。
    LLM_STREAM_USAGE: bool = True

    # ========== Redis 配置 (可选) ==========
    REDIS_URL: Optional[str] = None

    # ========== Embedding 配置 ==========
    # 独立的 Embedding API 配置;留空则回退到 LLM_API_KEY / LLM_BASE_URL
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_MODEL: str = "embedding-2"
    # 单次 embeddings 请求最多提交多少条文本,长文档分批发送
    EMBEDDING_BATCH_SIZE: int = 32
    RAG_MIN_SCORE: float = 0.3
    RAG_TOP_K: int = 5
    # 开启时在模型首轮之前先做一次检索并注入结果(可靠但每轮固定消耗一次检索);
    # 关闭则为纯 agentic RAG,完全由模型自主决定何时、以什么查询检索。
    RAG_PREFETCH: bool = True

    # ========== Agent 循环配置 ==========
    # 单次回答中允许的最大模型轮次。最后一轮不再提供工具,强制模型给出最终回答,
    # 因此实际可用的工具轮次为 AGENT_MAX_TOOL_ROUNDS - 1。
    AGENT_MAX_TOOL_ROUNDS: int = 6
    # 单个工具结果注入上下文的字符上限
    TOOL_RESULT_MAX_CHARS: int = 4000
    # 一次回答中所有工具结果的总字符预算,防止多轮累积撑爆上下文窗口
    TOOL_RESULT_TOTAL_CHARS: int = 12000

    # ========== 分块配置 ==========
    # token 计数器: heuristic(零依赖估算) | tiktoken(精确,需额外安装且首次会下载词表)
    TOKEN_COUNTER: str = "heuristic"
    CHUNK_MAX_TOKENS: int = 320
    # 仅在单个超长块被硬切时生效;跨段落上下文由检索阶段的邻域扩展补全
    CHUNK_OVERLAP_TOKENS: int = 40

    # ========== 检索管线 ==========
    # 稠密向量 + BM25 双路召回后用 RRF 融合。关闭则退化为纯向量检索,便于对照
    RAG_HYBRID: bool = True
    # 每条召回通道各取多少候选进入融合
    RAG_CANDIDATES_PER_CHANNEL: int = 20
    # 命中分块前后各带几个相邻分块,补全被切断的上下文(0 表示关闭)
    RAG_CONTEXT_WINDOW: int = 1
    # 多查询改写:提召回,代价是每次检索多一次模型调用
    RAG_MULTI_QUERY: bool = False
    RAG_MULTI_QUERY_COUNT: int = 2
    # LLM listwise 重排:提精度,代价是每次检索多一次模型调用
    RAG_RERANK: bool = False
    RAG_RERANK_CANDIDATES: int = 20
    RAG_RERANK_SNIPPET_CHARS: int = 500

    # ========== 对话历史 ==========
    # 历史消息的 token 预算(不含系统提示词、当前问题与预留的输出空间)
    HISTORY_TOKEN_BUDGET: int = 4000
    # 每轮从数据库取回多少条历史备选,再由 token 预算决定保留几条
    HISTORY_FETCH_LIMIT: int = 80
    # 超出预算的早期历史是否压成滚动摘要(关闭则直接丢弃)
    HISTORY_SUMMARY: bool = True
    HISTORY_SUMMARY_MAX_TOKENS: int = 400

    # ========== 可观测性 ==========
    # 关闭后所有埋点退化为无副作用的空操作,不写库
    TELEMETRY_ENABLED: bool = True
    # 单条 span 的 attributes JSON 上限。埋点只存元数据,不存提示词与用户文本,
    # 这个上限是防止将来误加字段时把整段上下文写进库的兜底。
    TELEMETRY_ATTR_MAX_CHARS: int = 2000
    # 价目表 JSON 路径(相对 back-end/)。缺失时成本一律为"未知"而不是编一个数字。
    PRICING_CONFIG_PATH: str = "model_prices.json"
    # 用量查询的默认统计窗口(天)
    METRICS_DEFAULT_DAYS: int = 7

    @property
    def embedding_api_key(self) -> str:
        """实际使用的 Embedding API Key (优先 EMBEDDING_API_KEY,回退 LLM_API_KEY)"""
        return self.EMBEDDING_API_KEY or self.LLM_API_KEY

    @property
    def embedding_base_url(self) -> str:
        """实际使用的 Embedding Base URL (优先 EMBEDDING_BASE_URL,回退 LLM_BASE_URL)"""
        return self.EMBEDDING_BASE_URL or self.LLM_BASE_URL

    # ========== 文件上传配置 ==========
    UPLOAD_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")

    # ========== JWT 认证配置 ==========
    JWT_SECRET_KEY: str = _DEFAULT_JWT_KEY
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 10080  # 兼容旧配置,实际使用下面的分项
    JWT_ACCESS_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_EXPIRE_DAYS: int = 7

    # ========== CORS 白名单 ==========
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:4173,http://127.0.0.1:5173"

    class Config:
        """Pydantic 配置"""
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        env_file_encoding = "utf-8"
        case_sensitive = True

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"


settings = Settings()

# 启动时校验 JWT 密钥不能为默认占位符
if settings.JWT_SECRET_KEY == _DEFAULT_JWT_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY 仍为默认占位符,请在 .env 中配置一个长度 >= 32 字符的随机字符串"
    )
