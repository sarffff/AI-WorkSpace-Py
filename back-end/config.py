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

    # ========== 工具轨迹（跨回合记忆） ==========
    # 回合内工具结果是靠 messages 回灌的,回合结束那个列表就没了,落库的只有
    # 最终回答。开启后把每步工具执行存进 message_tool_steps,下一回合按预算
    # 回灌成一段记录,模型才知道自己上一回合读过什么。
    # 关掉即退回"每个回合从零开始",可作为对照。
    TOOL_HISTORY_ENABLED: bool = True
    # 回灌的 token 预算,超出的部分从最旧的步骤开始丢
    TOOL_HISTORY_TOKEN_BUDGET: int = 600
    # 单步摘要的字符上限。给得太小就只剩工具名,给得太大不如让模型重新调一次工具
    TOOL_HISTORY_STEP_CHARS: int = 240
    # 每回合从数据库取回多少条备选步骤,再由 token 预算决定留几条
    TOOL_HISTORY_FETCH_LIMIT: int = 20
    # 单步结果落库的字符上限。存的是原始正文,不是摘要
    TOOL_HISTORY_STORE_MAX_CHARS: int = 4000

    # ========== Workspace 工具 ==========
    # 知识库那三个工具由界面上的「知识库」开关(use_rag)控制,这里几个各自独立:
    # 查网页、算数、读附件都不需要知识库,绑在同一个开关上等于关掉知识库就没了
    # 计算器。默认全部关闭——打开一个工具就是把它的失败模式和攻击面一起打开。
    #
    # 打开之后建议把 PROMPT_CHAT_SYSTEM_VERSION 切到 v4-workspace:默认的 v2
    # 只讲了知识库那三个工具,新工具全靠 schema 里的 description 自己撑着。
    TOOL_CALCULATE_ENABLED: bool = False
    TOOL_READ_ATTACHMENT_ENABLED: bool = False
    TOOL_WEB_SEARCH_ENABLED: bool = False
    # 唯一的写操作。默认关闭不是保守:内容可能是模型转述的网页,写进知识库就等于
    # 让注入内容获得持久化,并在之后每一轮 RAG 里被复用。
    TOOL_WRITE_KNOWLEDGE_ENABLED: bool = False
    AGENT_WRITE_MAX_CHARS: int = 20000

    # 读附件:单个文件的字节上限与注入上下文的字符上限
    ATTACHMENT_READ_MAX_BYTES: int = 5 * 1024 * 1024
    ATTACHMENT_READ_MAX_CHARS: int = 8000

    # web 搜索。provider 为空或缺 API key 时这个工具**根本不注册**
    WEB_SEARCH_PROVIDER: str = ""  # tavily | serper
    WEB_SEARCH_API_KEY: str = ""
    # 留空则用提供商默认端点;填了可指向自建代理或区域端点
    WEB_SEARCH_BASE_URL: str = ""
    WEB_SEARCH_RESULTS: int = 5
    WEB_SEARCH_SNIPPET_CHARS: int = 300
    WEB_SEARCH_TIMEOUT_SECONDS: float = 10.0

    # ========== 视觉 ==========
    # 能接收 image_url 内容块的模型白名单(逗号分隔)。留空即关闭多模态,
    # 图片仍以 Markdown 链接留在提示词里(也就是模型看不见)。
    # 用白名单而不是猜名字:模型命名毫无规律,猜错的代价是每个带图请求都拿到 400。
    VISION_MODELS: str = ""
    # 单张图片的字节上限。base64 会把体积放大三分之一,直接决定请求体大小
    VISION_MAX_IMAGE_BYTES: int = 4 * 1024 * 1024
    # 一轮最多带几张。图片按面积折算 token,一张高清图能顶几千字
    VISION_MAX_IMAGES: int = 4

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

    # ========== 语义缓存 ==========
    # 默认关闭:嵌入向量对时间、否定这类"改变答案"的差异不敏感,
    # 命中一条相似但不同的问题会直接答错。开启即接受这个取舍。
    SEMANTIC_CACHE_ENABLED: bool = False
    # 余弦相似度阈值。这个数应该由评估集扫出来,不是拍脑袋定的
    SEMANTIC_CACHE_THRESHOLD: float = 0.95
    SEMANTIC_CACHE_TTL_SECONDS: int = 86400
    # 每个用户最多缓存多少条(进程内存储,超量丢最旧)
    SEMANTIC_CACHE_MAX_ENTRIES: int = 200

    # ========== 安全护栏 ==========
    # 关闭后检索内容原样拼进提示词(只建议在排查护栏误报时临时关闭)
    GUARDRAIL_ENABLED: bool = True
    # 注入模式累计分数达到该值时,整段检索结果不再注入(0 = 只标记不拦截)。
    # 默认只观测:误报的表现是"明明有资料却答不出来",比漏报更难排查,
    # 先在 trace 里看一段时间命中情况再决定收紧到多少。
    GUARDRAIL_BLOCK_SCORE: int = 0

    # ========== 提示词版本 ==========
    # 实际正文在 back-end/prompts/<key>/<version>.md,这里只选用哪一版。
    # 之所以做成配置项:提示词是改动最频繁的那部分"代码",而"换一版提示词"
    # 必须能像换检索开关一样被 eval/variants.py 扫,否则只能靠感觉调词。
    PROMPT_CHAT_SYSTEM_VERSION: str = "v2"
    PROMPT_EVAL_ANSWER_VERSION: str = "v1"

    # ========== 时区 ==========
    # 应用写入 naive DATETIME 列时使用的时区偏移(小时)。必须与数据库服务器的
    # 墙上时间一致,否则 server_default=func.now() 写的行和应用写的行会差一个时区。
    APP_TZ_OFFSET_HOURS: int = 8

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
