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
    LLM_MODEL: str = "glm-4.6v"
    # 流式请求附带 stream_options.include_usage 以拿到真实 token 用量。
    # 部分 OpenAI 兼容端点不认这个参数,被拒一次后会自动停发并改用本地估算。
    LLM_STREAM_USAGE: bool = True

    # ========== 模型路由 ==========
    # 辅助任务(历史摘要/查询改写/重排/指代消解/记忆抽取)用的便宜模型。
    # 留空回退 LLM_MODEL。这些任务对推理能力要求低、调用频次高,与主回答
    # 同价是纯浪费——埋点里的 purpose 字段早就区分了用途,缺的只是这张路由表。
    LLM_UTILITY_MODEL: str = ""
    # 裁判模型。LLM-as-judge 的底线是裁判与被评模型分开:同一个模型给自己打分
    # 存在系统性的自我偏好(self-preference bias),变体对比的结论会被污染。
    # 留空依次回退 LLM_UTILITY_MODEL / LLM_MODEL。
    JUDGE_MODEL: str = ""

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
    # 预检索前先把本轮问题结合近期历史改写成自包含问题(指代消解)。
    # "那它的赔偿标准呢?"这类省略式追问,拿原文去检索会结构性召回漂移;
    # 而系统提示词又告诉模型"预检索过了,够用就直接答",等于把弱检索的结果
    # 包装成"已查过"。仅在存在历史消息时才发起,首轮不额外花钱。
    RAG_CONDENSE_QUERY: bool = True

    # ========== Agent 循环配置 ==========
    # 单次回答中允许的最大模型轮次。最后一轮不再提供工具,强制模型给出最终回答,
    # 因此实际可用的工具轮次为 AGENT_MAX_TOOL_ROUNDS - 1。
    AGENT_MAX_TOOL_ROUNDS: int = 6
    # 单个工具结果注入上下文的字符上限
    TOOL_RESULT_MAX_CHARS: int = 4000
    # 一次回答中所有工具结果的总字符预算,防止多轮累积撑爆上下文窗口
    TOOL_RESULT_TOTAL_CHARS: int = 12000
    # 同一个 (工具, 参数) 在一次回答里最多执行几次,第 N 次起不再执行,改为回灌
    # 一句纠正说明。轮次上限和字符预算都管不到这件事:重复调用每次都是合法调用、
    # 都在预算内,只是拿回来的东西一模一样。0 表示关闭检测(退回改动前的行为)。
    #
    # 为什么算总次数而不是"连续三次":A、B、A、B、A 这种在两个相同查询之间来回
    # 摆的情况是同一种病,而只看连续完全抓不到它。
    AGENT_REPEAT_LIMIT: int = 3

    # ========== 多代理协作（委派） ==========
    # off        : 单代理,与此前逐位相同(默认)
    # augment    : 主代理保留全部工具,额外多一个 delegate。它可以自己做也可以派人,
    #              于是"什么时候值得委派"由模型判断——这是能看出委派有没有用的模式。
    # supervisor : 专用工具从主代理手里收走,只留 delegate 与不属于任何角色的工具。
    #              分工更干净,代价是简单问题也得多付一次生成。
    #
    # 默认 off 不是保守:委派会把一次回答的模型调用次数变成不确定的(每次委派多一次
    # 完整的子代理循环),成本与延迟都随之上升。开之前应该先在 traces 里看清
    # 单代理模式下究竟是哪一步不够用。
    AGENT_DELEGATION_MODE: str = "off"
    # 一次回答里最多委派几次。没有这个上限时主代理可以每一轮都派一次人,
    # 而每一次都是一个完整的嵌套循环——AGENT_MAX_TOOL_ROUNDS 管不到它。
    AGENT_MAX_DELEGATIONS: int = 3

    # ========== 显式规划(plan-and-execute) ==========
    # off          : 纯 ReAct,与加这个功能之前逐位相同(默认)
    # plan_execute : 进循环之前先让辅助模型把问题拆成有序步骤,计划作为一条
    #                指引注入,然后照常跑现在这个执行循环
    #
    # 为什么不换掉现在的循环:ReAct 的"边走边看"在工具结果不确定时是优势——
    # 检索空了就换查询,这件事计划里写不出来。plan_execute 加的是**事前的全局
    # 视野**,它该压住的是"一次只想一步、于是绕路"。两者不是替代关系,所以这里
    # 是在同一个执行循环前面加一段,而不是另开一条代码路径。
    #
    # 已知失效模式写在提示词里(prompts/agent_plan/):给简单问题硬凑几步。所以
    # 空计划是合法输出,而 planAdherence 这个指标专门盯"计划了却没照做"。
    #
    # 收益无法先验断定,只能量:规划本身是一次额外的辅助模型调用,而这个仓库里
    # 已经有一半的"增强"被证明零收益甚至没执行过。所以 eval 里有 plan-execute
    # 变体,和 delegation-* 一样,先量再说。
    AGENT_PLAN_MODE: str = "off"
    # 计划最多几步。上限是**校验**不是截断:多出来的步骤砍掉等于把"模型没照
    # max_steps 做"翻译成"计划就这么长"(见 structured.Plan)。
    AGENT_PLAN_MAX_STEPS: int = 5
    # 规划那次调用的输出预算。默认给到 2048 而不是"够输出五行 JSON 就行"——
    # 本仓库 2026-08-22 实测七个辅助调用点有五个因为按输出长度定预算而 100%
    # 返回空串(推理模型先花预算思考)。这里从第一天就按思考开销定,
    # 而不是等它静默失效一次。判据见 scripts/probe_structured_budgets.py。
    AGENT_PLAN_MAX_TOKENS: int = 2048

    # ========== 状态快照与中断恢复 ==========
    # 关掉即退回"一个回合的状态只活在 SSE 生成器的局部变量里":连接一断就没了,
    # 也就没有人工审批(它要跨请求)、没有重放、没有 agent_runs 记录。
    #
    # 打开的代价是实打实的:每轮工具执行前写一份快照,而快照就是整个 messages
    # 列表。六轮下来几十 KB,所以有 AGENT_CHECKPOINT_KEEP 兜着。
    AGENT_CHECKPOINT_ENABLED: bool = False
    # 每个 run 保留最近几份快照,0 表示不清理。留多份是为了重放("回到第 3 轮
    # 再跑一次"),只留最新一份的话恢复就只有"继续"一个方向。
    AGENT_CHECKPOINT_KEEP: int = 8
    # 等待审批的执行多久算废弃(小时)。超时的 run 不会自动执行也不会自动拒绝,
    # 只是从"待审批"列表里消失——自动裁决比一直挂着更危险。
    AGENT_APPROVAL_TIMEOUT_HOURS: int = 24

    # ========== 人工审批 ==========
    # off    : 不审批(默认)。破坏性操作仍受确认令牌约束(见 workspace_tools)
    # write  : 写操作要人点一下同意(save_to_knowledge_base / delete_knowledge_document)
    # listed : 完全由 AGENT_APPROVAL_TOOLS 决定,一个都不隐含
    #
    # 依赖 AGENT_CHECKPOINT_ENABLED:审批要等用户在**另一个请求**里点同意,
    # 没有快照就没有东西可恢复。两个都开才生效。
    AGENT_APPROVAL_MODE: str = "off"
    # listed 模式下的工具名,逗号分隔。给 web_search 加审批能立刻看出
    # "每一步都要点同意"的体验代价——这件事讲道理讲不清,试一次就清楚了。
    AGENT_APPROVAL_TOOLS: str = ""

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

    # ========== 工具调用外围约束 ==========
    # 单个工具在一次回答里连续失败（参数错误或通道故障）多少次后，从本轮 schema
    # 里移除，之后模型再调它只会得到一句"已熔断"。0 表示关闭。
    #
    # 连续而不是累计：偶尔一次参数写错很常见，熔断针对的是"同一个工具反复失败"——
    # 那才是幻觉或通道故障的信号，继续让它试只会把剩下的轮次烧光。而它的处置是
    # **移除 schema** 而不是拒绝执行：模型根本看不到这个工具，就不会再发起调用，
    # 比"每轮都试一次、每次都拿回一句拒绝"更省轮次，也更好观测。
    TOOL_CIRCUIT_BREAKER_FAILURES: int = 2
    # 删除知识库文档。破坏性写操作，默认关闭；打开后还要求用户在对话里明确说过
    # 要删（确认令牌，见 workspace_tools._ToolApprovals），缺一不执行。
    TOOL_DELETE_KNOWLEDGE_ENABLED: bool = False
    # 澄清工具：模型拿不准用户意图或关键参数缺失时，把问题抛回给用户，而不是
    # 硬猜一个参数去调工具。代价是每个澄清问题要等用户回话，回合在此终止。
    TOOL_ASK_USER_ENABLED: bool = False
    # 网页抓取：把模型给出的 URL 抓成纯文本再过护栏。和 web_search 的区别是
    # 抓正文而不是看摘要，SSRF 面也因此更大，默认关闭。
    TOOL_WEB_FETCH_ENABLED: bool = False
    # 单个页面允许读取的字节上限（超出即判失败）与注入上下文的字符上限
    WEB_FETCH_MAX_BYTES: int = 200 * 1024
    WEB_FETCH_MAX_CHARS: int = 8000
    WEB_FETCH_TIMEOUT_SECONDS: float = 10.0

    # ========== 视觉 ==========
    # 能接收 image_url 内容块的模型白名单(逗号分隔)。留空即关闭多模态,
    # 图片仍以 Markdown 链接留在提示词里(也就是模型看不见)。
    # 用白名单而不是猜名字:模型命名毫无规律,猜错的代价是每个带图请求都拿到 400。
    VISION_MODELS: str = ""
    # 单张图片的字节上限。base64 会把体积放大三分之一,直接决定请求体大小
    VISION_MAX_IMAGE_BYTES: int = 4 * 1024 * 1024
    # 一轮最多带几张。图片按面积折算 token,一张高清图能顶几千字
    VISION_MAX_IMAGES: int = 4

    # ========== 摄取层清洗 ==========
    # 脏输入不是"质量差一点",它会让召回通道整条失效:GBK 文档被 errors="replace"
    # 变成一串 U+FFFD 之后,retrieval_index.tokenize() 两个正则都匹配不到,BM25
    # 建索引时直接跳过整块——稀疏通道彻底看不见这篇文档,而状态还是 indexed。
    # 关掉即退回改动前的行为(硬 utf-8 解码 + PyPDF2 纯文本抽取),作为对照组。
    INGEST_CLEAN: bool = True
    # 非 utf-8 文档的解码先验(逗号分隔,按顺序严格试)。
    #
    # 为什么需要先验而不是纯靠嗅探:同一串中文字节在 GB18030 / EUC-KR / Shift-JIS
    # 下**都能严格解通**,从字节本身分辨不了,任何检测器都不行——实测一段 GBK 短句
    # 会被 charset-normalizer 判成 EUC-KR,解出来是一串谚文。而猜错编码时解出的文本
    # 没有任何替换符,INGEST_MIN_TEXT_RATIO 那道自检也抓不到它。
    #
    # gb18030 是 GBK / GB2312 的超集,一条就够。代价说清楚:一份真正的韩文文档会被
    # 当成中文解错。对这个项目(中文语料、中文用户)这是正确的取舍,但它是取舍。
    INGEST_ENCODING_HINTS: str = "gb18030"
    # 用 pdfplumber 的字号与坐标恢复标题层级、剔页眉页脚、修词内空格。
    # 这是让 chunking 那四件事对 PDF 重新生效的唯一途径:PyPDF2 只给字符串,
    # 没有字号,标题层级怎么正则都推不出来。关掉则走 PyPDF2 纯文本路径。
    INGEST_PDF_STRUCTURE: bool = True
    # 可读字符占比(1 − U+FFFD 替换符占比)低于此值即判 failed。
    #
    # 2026-08-23 从 0.6 提到 0.9。原来的理由是"真正的编码错误会低到接近 0",
    # **实测不成立**:GBK 文档硬解 utf-8 之后 ratio 落在 0.2755–0.6547,上界取决于
    # 文档里有多少 ASCII(代码标识符、数字、表格竖线全都完好无损)。技术文档 ASCII
    # 多,于是中文全毁也能拿到 0.65。有一篇正好 0.6011,**过了 0.6 这道门**——
    # 状态是 indexed、界面上和正常文档没差别,而 BM25 只切出 11 个 token(原文 47)
    # 且全是 ASCII 残留,中文查询一个字都命中不了。
    #
    # 提到 0.9 是安全的:U+FFFD 在真实内容里没有任何合理来源,出现一个就说明解码
    # 坏了。实测正常语料、pdf_like、noisy_unicode 三类**全是 1.0000**,与坏文档
    # 之间是大片空白,0.9 落在空白中间,零误伤。
    #
    # 它抓不到的两类要知道:(1) 编码猜错但解通的文本没有替换符(见
    # INGEST_ENCODING_HINTS);(2) 扫描件 ratio 也是 1.0000 但 token 为 0,
    # 由 index_document 的 no_chunks 自检兜。
    INGEST_MIN_TEXT_RATIO: float = 0.9
    # 页眉页脚的判据是"在至少这么多页的相同边缘区域重复出现"。
    # 按位置单独判会把首页正文第一行也剔掉,所以必须要有跨页重复这个条件。
    INGEST_HEADER_FOOTER_MIN_PAGES: int = 3
    # 索引完成后随机抽一块做一次自检索,命中不了自己就记一条 warning。
    # 这是把"索引成功"的判据从"没抛异常"换成"真的检索得到"——静默失败的那几种
    # (空文本、乱码、维度不匹配)全都不抛异常。代价是每篇文档多一次 embedding 调用。
    INGEST_SELF_CHECK: bool = True
    # 评估语料的降级方式:none | pdf_like | gbk_bytes | scanned(见 eval/corpus_degrade.py)。
    # 只影响离线评估,线上永远是 none。
    #
    # 它存在的理由是一个盲区:eval/corpus/ 下 6 篇是自造的干净 utf-8 Markdown,
    # 于是这套评估**量不出任何清洗改动的价值**——清洗代码根本没有输入可清。
    # 降级把干净语料变成"真实上传"的样子,再和清洗开关配对跑,差值就是清洗值多少。
    EVAL_CORPUS_DEGRADE: str = "none"

    # ========== 分块配置 ==========
    # token 计数器: heuristic(零依赖估算) | tiktoken(精确,需额外安装且首次会下载词表)
    TOKEN_COUNTER: str = "heuristic"
    CHUNK_MAX_TOKENS: int = 320
    # 仅在单个超长块被硬切时生效;跨段落上下文由检索阶段的邻域扩展补全
    CHUNK_OVERLAP_TOKENS: int = 40
    # 分块策略: structural | semantic
    #   structural — 按 Markdown 结构(标题层级、代码围栏、段落)切,默认,零成本。
    #   semantic   — 按句切开后算相邻句向量的余弦距离,在距离突变处断开。
    #                好处是话题真正转折的地方才断,而不是恰好写了个空行的地方;
    #                代价是入库时每篇文档多一批 embedding 调用。
    #
    # 为什么不做 late chunking:那需要 token 级 hidden states 再按块池化,而
    # /embeddings 每条输入只返回一个池化后的向量,hosted API 拿不到 token 级输出。
    CHUNK_STRATEGY: str = "structural"
    # 相邻句距离取这个分位数作断点阈值。95 表示只在最"跳"的那 5% 处断开。
    # 用分位数而不是绝对阈值:余弦距离的绝对值随 embedding 模型变,换个模型
    # 绝对阈值就得重调,而分位数是自适应的。
    CHUNK_SEMANTIC_PERCENTILE: float = 95.0
    # 语义分块的最小句数。太短的文档没有可统计的距离分布,直接走 structural
    CHUNK_SEMANTIC_MIN_SENTENCES: int = 6

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
    # 重排方式: off | llm | api
    #   llm — 现有的 LLM listwise:把候选编号列给通用模型让它排序。留着当对照组,
    #         能量出"专用 cross-encoder 比让通用模型排序好多少"。
    #   api — 专用 rerank 接口。query 与 document 拼在一起过一遍模型输出标量
    #         相关度,这是它比稠密检索准的原因:稠密是 bi-encoder,两侧各自独立
    #         编码,编码时从未见过对方。
    # 留空则按 RAG_RERANK 布尔量决定(True → llm),保持现有变体与测试不变。
    RAG_RERANK_MODE: str = ""
    RERANK_MODEL: str = "rerank"
    # 留空回退 LLM_BASE_URL / LLM_API_KEY —— 同一家提供商时 rerank 与对话共用凭证。
    #
    # ⚠️ 2026-08-23 实测:智谱 /rerank 返 **429 / code 1113**(账号无该项额度),
    # 而 chat 用同一个 key 是 200。诊断方式是换个模型名对比——`rerank` 报
    # 429/1113、`rerank-2` 报 400/1211(模型不存在),说明模型名对、是额度问题。
    # 后果:mode=api 在智谱上等于**静默降级成融合序**,报告里看着就是"专用重排
    # 没有增益"。rerank-api 变体因此长期从未真正执行过。
    #
    # 可用的替代:SiliconFlow 的 BAAI/bge-reranker-v2-m3,非 Pro 层免费。配法是
    #     RERANK_BASE_URL=https://api.siliconflow.cn/v1
    #     RERANK_API_KEY=<SiliconFlow key>
    #     RERANK_MODEL=BAAI/bge-reranker-v2-m3
    # 不把它写成默认值:默认值指向一个需要另一家凭证的服务,没配 key 的人会从
    # "不启用重排"变成"启用了但每次请求都 401"。默认仍是同源回退,由 .env 决定。
    RERANK_BASE_URL: str = ""
    RERANK_API_KEY: str = ""
    RERANK_TIMEOUT_SECONDS: float = 10.0

    # 辅助模型调用（重排 / HyDE / 多查询改写 / 摘要）的超时与重试上限。
    #
    # 2026-08-27 加。此前 ``AsyncOpenAI`` 是不带这两个参数构造的，于是吃 SDK 默认：
    # **read=600s、max_retries=2 → 最坏一次调用 1800 秒**。实测 rerank 变体
    # p90 延迟 255 秒、最大 336 秒（baseline 是 37 秒），那个 336 就是重试链。
    #
    # 为什么辅助调用要单独设一个更短的值：它们**全都有降级路径**——重排失败退回
    # 融合序、HyDE 失败用原查询。为一个可以放弃的增强等 10 分钟是纯亏，而且拖慢
    # 的是用户正在等的那次回答。主回答的调用不受这个约束（用户确实在等它）。
    #
    # 60 秒的依据：实测一次 20 候选的 listwise 重排约 32 秒成功，60 秒给一倍余量。
    # 重试 1 次而不是 2：辅助调用失败退回降级路径比多试一次更划算。
    LLM_AUXILIARY_TIMEOUT_SECONDS: float = 60.0
    LLM_AUXILIARY_MAX_RETRIES: int = 1

    # 主回答调用的上界。2026-08-29 加。
    #
    # 上面那条注释说「主回答的调用不受这个约束(用户确实在等它)」——那个判断对,
    # 但它得出的结论应该是「给一个更宽松的超时」,而不是「不给超时」。不给的结果是
    # 落回 SDK 默认 600s × 3 = **最坏 1800 秒**,而这条路径上挂着的东西比辅助调用
    # 多得多:一个数据库会话、一条 SSE 连接、以及 Agent 循环最多 6 轮里的这一轮。
    # 连接池打满就是全站不可用,而不只是这一次回答变慢。
    #
    # **流式下这个值的语义是「两个分片之间最多等多久」,不是整条流的总时长。**
    # httpx 的 read timeout 在每次读之间重置,所以正常输出的长回答不会被这个值切断
    # (每个分片都在刷新计时),真正被它拦住的是「连上了但不再吐字节」的挂死连接
    # ——那恰好是我们要防的。整条流的总时长另有 AGENT_MAX_TOOL_ROUNDS 与
    # 结果字符预算兜着。
    #
    # 120 秒的依据:实测 baseline 一次对话调用 p90 约 37 秒(见上面 EVAL 那段的实测
    # 数据),给三倍余量。带思考链的推理模型首字延迟更长,所以不取更小的值。
    #
    # 重试 2 次:与辅助调用不同,主回答**没有降级路径**——失败就是用户看到报错,
    # 所以这里值得多试。安全性见 model_adapter._open_stream 的注释:重试发生在
    # 开流之前,不会让用户看到重复内容。
    LLM_CHAT_TIMEOUT_SECONDS: float = 120.0
    LLM_CHAT_MAX_RETRIES: int = 2

    # ---- 评估链路的限流与 429 退避(eval/runner) -------------------------------
    #
    # 2026-08-27 那轮 3 变体评估打了 **211 次 HTTP 429**:评估一次性在几分钟内
    # 打出几百次对话调用(~问题数×变体数×2,外加重试),没有真实用户节奏兜底,
    # 很容易把账号的每分钟配额打满。线上靠人肉节奏天然散开,评估没有——所以
    # 在这里显式节流,而不是去改 LLM_AUXILIARY_*(那会影响线上辅助调用)。
    #
    # 这三个开关只作用于 eval/runner 里的 _LLMRateGate:评估跑完不再需要,
    # 设默认值即可,不设也不会影响线上路径。
    #
    # min_interval:每次 LLM 调用之间的最小间隔。0 关闭(不加间隔)。
    #   它的作用是**防突发**,不是限速——每次调用本身要花好几秒,间隔只在
    #   配额已耗尽(服务端秒拒)时才真正起作用。
    # cooldown:撞上 429 且服务端没给 Retry-After 时,退避多少秒再重试。
    # max_retries:撞上 429 后重试几次(含按配额窗口退避)。默认 12 次是给
    #   一分钟配额窗口留下足够余量;retries 是给「重试次数」的兜底。
    EVAL_LLM_MIN_INTERVAL_SECONDS: float = 0.5
    EVAL_LLM_RATE_LIMIT_COOLDOWN_SECONDS: float = 8.0
    EVAL_LLM_MAX_RATE_RETRIES: int = 12

    # HyDE(Hypothetical Document Embeddings):先让辅助模型编一段假答案,拿它去
    # 检索。反直觉但有效——用户的问题和文档的措辞常常不在同一个语域("报销要几天"
    # vs "费用审批时限"),而一段假答案的措辞天然更接近文档。
    #
    # 关键实现约束:假答案**只喂稠密通道**,BM25 继续用原始 query。两路都换等于
    # 亲手废掉稀疏通道:假答案里的专有名词、编号、错别字全是模型编的,拿它做字面
    # 匹配只会命中一堆无关内容。
    RAG_HYDE: bool = False
    # 实测原值 200 不够:HyDE 的正确输出只有 88 字,但思考先把 200 花光,返回空串,
    # 于是 hyde 变体与 baseline 逐位相同。见下面那组"辅助调用的输出预算"。
    RAG_HYDE_MAX_TOKENS: int = 2048
    # 查询路由:让辅助模型判断这个查询偏字面还是偏语义,据此调整两路的 RRF 权重。
    # eval 数据集的 probe 标注(lexical / paraphrase / table_lookup ...)天生就是
    # 这个分类器的标注集,所以它的准确率是可测的,不用凭感觉。
    RAG_QUERY_ROUTE: bool = False
    # RRF 的默认通道权重。路由关闭时两路都是 1.0,与改动前逐位相同。
    RAG_RRF_DENSE_WEIGHT: float = 1.0
    RAG_RRF_SPARSE_WEIGHT: float = 1.0
    # 路由判定为偏字面/偏语义时,弱侧通道的权重降到多少
    RAG_ROUTE_WEAK_WEIGHT: float = 0.4

    # ---------- 辅助调用的输出预算 ----------
    # 这一组存在的理由是两次实测,而不是"参数最好都可配"。
    #
    # 第一次(scripts/probe_structured_budgets.py):量全库 7 个辅助模型调用点,
    # 同一段提示词跑现有预算和 2048 两次,**5 个在现有预算下 100% 返回空串**。
    # 原因是 glm-4.5-air 这类混合推理模型会先花 max_tokens 思考,预算不够时
    # 一个字都不吐——不是截断到一半的 JSON,是空串。
    #
    # 空串之后的链路每一环都"合理":解析不出 JSON → 调用方按"这是增强不是依赖"
    # 静默降级 → 没有日志。叠起来的结果是这四个检索增强从来没有执行过,而
    # eval/variants.py 里对应的 5 个变体与 baseline 逐位相同——报告上读作
    # "这个技术没有增益",真相是它没跑。
    #
    # 第二次(scripts/sweep_worst_case_budgets.py):第一次的结论是**假绿灯**。
    # 那个探针用玩具输入(一个短问题、5 个候选),而思考开销跟着输入长度涨。
    # 按各自的输入上界重量之后,统一配 1024 的三个调用点仍然 100% 失败:
    #
    #   rerank         提示词 10241 字(20 候选 × 500)  最低 3072
    #   query_condense 提示词  2653 字(6 轮 × 400)     最低 2048
    #   history_summary提示词 15945 字(无硬截断!)      最低 2048
    #   memory_extract 提示词  6322 字(2000 + 4000)    最低 1024
    #
    # 教训不是"1024 不够",是**按典型输入定预算会漏掉配置放大的那一档**。
    # 所以下面每个值都留一倍余量:候选数、历史预算都是可配的,贴着最低值配
    # 等于把"改大那个配置"变成一个静默失效的开关。
    #
    # 为什么可以大方给:max_tokens 是**上界不是账单**。计费按真实输出 token,
    # 而失效那一侧的代价是"思考的 token 全额付费、拿回来一个空串"——所以
    # 宁大勿小在这里连成本权衡都不算。
    #
    # 现在这类失效不再无声:finish_reason=length 且正文为空会在 model_adapter
    # 里记 span 并打 warning(见 _record_truncation),structured 层再补一条
    # ``truncated`` 失败标签和 ``budget_exhausted`` 埋点。
    #
    # 这三个的输入是固定的短提示词(一个问题),量下来 1024 就够,但仍然给到
    # 2048 保持一致的余量——见上面"上界不是账单"。
    RAG_ROUTE_MAX_TOKENS: int = 2048
    RAG_MULTI_QUERY_MAX_TOKENS: int = 2048
    # 输入随 RAG_RERANK_CANDIDATES × RAG_RERANK_SNIPPET_CHARS 增长,是七个调用点里
    # 唯一输入会被配置放大的,也是唯一一个"按合成语料扫出来的下限不够用"的。
    #
    # 扫出来的最低值是 3072(20 候选 × 500 字的填充文本),按惯例给一倍余量配了
    # 6144。然后真实语料打回来了:2026-08-22 那轮 30 题 × 2 个重排变体里
    # **7 次仍然空串**(约 12%)。真实分块的信息密度比填充文本高,读 20 段要想的
    # 更多。所以这里按实测又翻了一倍。
    #
    # 这一条要记住的不是数字,是**输入会被配置放大的调用点必须按真实数据校准**,
    # 合成输入只能给下界。改动 RAG_RERANK_CANDIDATES 或 RAG_RERANK_SNIPPET_CHARS
    # 之后要重新跑一遍 scripts/sweep_worst_case_budgets.py,并盯住运行日志里
    # 有没有 "llm.rerank returned empty content"。
    RAG_RERANK_MAX_TOKENS: int = 12288
    # 指代消解。这一项是唯一**默认开启**的受害者(RAG_CONDENSE_QUERY=True),
    # 也就是说真实链路上每一次追问都在拿原文检索,而系统提示词还告诉模型
    # "已经预检索过了,够用就直接答"。最低 2048。
    RAG_CONDENSE_MAX_TOKENS: int = 4096

    # ========== 向量存储 ==========
    # memory | qdrant
    #
    # memory 是进程内 FAISS/numpy 索引:按工作区隔离、按签名失效、每次进程重启
    # 从 MySQL 重建。它的限制很具体——**多 worker 部署时每个 worker 各建一份**,
    # 于是一次上传之后哪个 worker 能检索到取决于请求打到了谁身上。
    #
    # qdrant 把向量搬成持久态:多 worker 共享、重启不丢、带 payload 过滤的 ANN。
    # 默认仍是 memory,切换是显式动作——它需要一个跑着的服务
    # (docker-compose.qdrant.yml),而"配置默认值悄悄要求一个外部依赖"是很坏的默认。
    VECTOR_STORE: str = "memory"
    # exact | hnsw。只作用于 memory 后端。
    #
    # exact 是暴力扫描(IndexFlatIP / numpy 矩阵乘),召回率恒为 100%。
    # hnsw 是近似最近邻,拿召回率换延迟——**在当前语料规模下它只会更差**:
    # 几千个向量的暴力扫描本来就是毫秒级,而 HNSW 引入了图构建开销和召回损失。
    # 它存在的意义是让"ANN 的代价"变成一个能量出来的数(recall@5 与 avgRetrievalMs
    # 一起看),而不是一句"到了大规模就该上 ANN"的口号。
    VECTOR_ANN: str = "exact"
    # HNSW 参数。M 是每个节点的出边数,ef_construction 是建图时的候选池大小,
    # ef_search 是查询时的候选池大小——三者都是"更大=更准更慢"。
    # 它们同时作用于 memory/hnsw 与 qdrant 两个后端,概念是同一套。
    VECTOR_HNSW_M: int = 16
    VECTOR_HNSW_EF_CONSTRUCT: int = 100
    VECTOR_HNSW_EF_SEARCH: int = 64

    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str = ""
    # 单 collection,workspace_id 存在 payload 里并建索引,查询时按它过滤。
    # 不做 collection-per-workspace:那会随用户数线性增长,而 Qdrant 的每个
    # collection 都有固定的内存与文件开销。
    QDRANT_COLLECTION: str = "ai_workspace_chunks"
    QDRANT_TIMEOUT_SECONDS: float = 5.0

    # ========== 对话历史 ==========
    # 历史消息的 token 预算(不含系统提示词、当前问题与预留的输出空间)
    HISTORY_TOKEN_BUDGET: int = 4000
    # 每轮从数据库取回多少条历史备选,再由 token 预算决定保留几条
    HISTORY_FETCH_LIMIT: int = 80
    # 超出预算的早期历史是否压成滚动摘要(关闭则直接丢弃)
    HISTORY_SUMMARY: bool = True
    # 这一项是七个调用点里输入**没有硬截断**的那个:滑出预算的历史有多少就压多少。
    # 按 HISTORY_TOKEN_BUDGET 量级构造(提示词 15945 字)实测最低 2048,给一倍余量。
    #
    # 这个数同时管两件互相冲突的事:给思考的余地,以及摘要本身的长度上限
    # (摘要要进 HISTORY_TOKEN_BUDGET,长了就挤掉真实历史)。API 只给一个旋钮,
    # 所以分工是:**这里给够思考**,长度由提示词那句"压缩成要点摘要"约束。
    # 代价要说清楚:2048 下最坏情况摘要是 530 字(约 350 token,占 4000 预算的 9%),
    # 4096 下可能更长。如果哪天摘要开始又长又啰嗦,该改的是
    # prompts/history_summary/ 里加一句字数上限,**不是把这个数调回去**——
    # 调小它只会让摘要重新变成空串(实测 400 和 1024 都失败)。
    HISTORY_SUMMARY_MAX_TOKENS: int = 4096

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

    # ========== 跨会话长期记忆 ==========
    # 每轮回答结束后用辅助模型从对话里抽取"值得跨会话记住"的用户事实与偏好,
    # 存进 user_memories 表,并在之后的每一轮作为系统上下文注入。
    # 这是"同一个用户每次都要重新自我介绍"和真正的个人助手之间的分水岭。
    MEMORY_ENABLED: bool = True
    # 每个用户最多保留多少条记忆,超量丢最旧
    MEMORY_MAX_ITEMS: int = 100
    # 每轮注入上下文的记忆条数上限(按最新优先)
    MEMORY_INJECT_LIMIT: int = 20
    # 单条记忆的字符上限,超长的"记忆"多半是把整段对话抄了一遍
    MEMORY_ITEM_MAX_CHARS: int = 200
    # 抽取那次辅助模型调用的输出上限。
    #
    # 这个数给小了会让整个记忆功能静默失效,而且完全看不出来:推理型模型
    # (glm-4.5-air 这类)会先花掉一部分预算做思考,预算不够时返回的 content 是
    # **空串**——于是 request_structured 连着两次拿到 no_json、返回 None,而
    # extract 对 None 的处理是"这轮不记"(抽取是增强不是依赖)。三层加起来的结果
    # 是:抽取 100% 不工作,日志干净,报告上只表现为"用户没有任何长期记忆"。
    # 原来写死的 512 正好落在失效那一侧,实测 1024 才刚够。
    #
    # 留出余量而不是贴着 1024:提示词里的 question/answer 各截到 2000/4000 字,
    # 长对话的思考开销更大。抽取每轮只跑一次,多给点输出预算比静默失效便宜。
    MEMORY_EXTRACT_MAX_TOKENS: int = 2048

    # ========== 提示词版本 ==========
    # 实际正文在 back-end/prompts/<key>/<version>.md,这里只选用哪一版。
    # 之所以做成配置项:提示词是改动最频繁的那部分"代码",而"换一版提示词"
    # 必须能像换检索开关一样被 eval/variants.py 扫,否则只能靠感觉调词。
    #
    # **默认值一律留空,它们是覆盖项而不是默认项。** 每一版的默认版本写在
    # prompt_library.SPECS 的 default_version 里——那儿紧挨着该 key 的占位符契约
    # 和用途说明,是唯一的事实来源。
    #
    # 这里曾经写死过具体版本号(比如 "v2"),后果是 resolve_version 那条
    # "显式传入 > settings > 契约默认值"的三层顺序里,第三层对**所有**带 setting
    # 的 key 都永远走不到:配置项非空,它就总是赢。于是 default_version 成了死字段,
    # 而同一个版本号在两个文件里各写一遍,改一处不改另一处不会有任何报错。
    #
    # 留空之后 .env 里写 PROMPT_CHAT_SYSTEM_VERSION= 也等于"用契约默认值",
    # 这个语义正是想要的。
    PROMPT_CHAT_SYSTEM_VERSION: str = ""
    PROMPT_EVAL_ANSWER_VERSION: str = ""
    # 子代理提示词的版本。三个角色各自一项——共享一个开关就没法单独 A/B 某个
    # 角色,动一个会让另两个的结果一起失效。
    #
    # 没有这三项之前,``role_prompt()`` 唯一的出口是 SPECS 里的 default_version,
    # 也就是说新版本只能靠改源码才能生效,eval 变体扫不到它——``prompt_key``
    # 那套版本化机制是空转的。
    PROMPT_AGENT_RESEARCHER_VERSION: str = ""
    PROMPT_AGENT_ANALYST_VERSION: str = ""
    PROMPT_AGENT_CRITIC_VERSION: str = ""

    # ========== 提示词缓存（provider 侧上下文缓存） ==========
    # 智谱等 OpenAI 兼容端点的上下文缓存是**隐式**的:没有 cache_control 断点、
    # 没有请求参数,提供商自己识别与之前请求相同的前缀并复用那部分计算,命中的
    # token 按标准价打折计费(智谱文档写的是约 50%)。因此工程上能做的只有一件事:
    # **让前缀真的逐字一致**。
    #
    # 开启后系统提示词按 prefetched=False 渲染,"已预检索过、不要重复检索"这句话
    # 改从用户消息里给(预检索没命中时那句提示本来就走这条路,见 chat_service)。
    # 关掉即回到改动前的行为——那时 messages[0] 会随预检索命中与否在两种正文之间
    # 来回切,而它是整个前缀的第一条消息,一变就是整段缓存作废。
    #
    # 留成开关而不是直接删掉模板里的 [[if prefetched]]:那一版条件段是"要不要在
    # 系统提示词里讲预检索"的对照组,和 TOOL_HISTORY_ENABLED / RAG_PREFETCH 一样,
    # 旧行为必须仍然跑得起来才能量出这个改动值多少。
    PROMPT_CACHE_STABLE_PREFIX: bool = True

    # ========== 结构化输出 ==========
    # 模型输出解析/校验失败时的重试次数。重试会把 Pydantic 的报错原文回灌给模型
    # 让它自己改——和工具参数校验失败时回灌 INVALID_ARGUMENTS 是同一个套路。
    # 0 表示不重试(解析失败即按各调用方的降级路径处理)。
    #
    # 默认只给 1 次:这些都是辅助任务(改写/重排/记忆抽取),失败的代价是"这次
    # 增强没生效",而不是回答出错。重试三次的钱花在主回答上更划算。
    STRUCTURED_OUTPUT_RETRIES: int = 1

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

    # ---- 用量闸门(services/usage_guard) ---------------------------------------
    #
    # 2026-08-29 加。此前 ``/chats/completions/stream`` 上一个上界都没有:
    # ``@limiter.limit`` 只挂在 auth 的两个端点上,而这条路径是全项目最贵的
    # ——最多 AGENT_MAX_TOOL_ROUNDS 轮模型调用,每轮可能带 web_search 与向量化。
    # 成本那边 telemetry 一直在算并写进 trace_spans.cost,但**没有任何一处因为
    # 成本拒绝执行**。两条合起来就是:一个脚本 = 无上界的花钱。
    #
    # 三个上界互相独立,分别防三件不同的事,全为 0 表示关闭:
    #
    # 1. **请求频率**(RATE):防滥用。进程内计数,每个请求都算,**不管有没有花钱**
    #    ——早早失败的请求也要算,否则打空请求的脚本永远撞不到上限。
    #    代价见 usage_guard 模块文档:多 worker 下每个进程各有一份计数。
    # 2. **成本**(COST):防超支。读 trace_spans.cost,也就是**真实记账**。
    # 3. **token**(TOKENS):成本的兜底。价目表里没有的模型 cost 是 NULL,
    #    只卡成本的话换个未定价模型就绕过去了;token 永远有记录(实测或本地估算)。
    #
    # 窗口都是滑动的,不按自然日对齐:按自然日对齐会在午夜给出一个整份的新配额,
    # 于是"卡住了就等到零点"变成一种可行的绕过方式。
    USAGE_GUARD_ENABLED: bool = True
    # 每用户每窗口的对话请求数上限。窗口用分钟计。
    USAGE_RATE_WINDOW_MINUTES: float = 1.0
    USAGE_RATE_MAX_REQUESTS: int = 20
    # 成本与 token 的窗口(小时)。默认 24 小时。
    USAGE_QUOTA_WINDOW_HOURS: float = 24.0
    # 每用户每窗口的成本上限,单位与价目表的 currency 一致。0 = 不限。
    #
    # 混币种时按**各币种独立比较**,不做汇率换算——项目里没有汇率来源,
    # 编一个换算率会得到一个看起来精确的假数字(同 pricing 模块的取舍)。
    USAGE_QUOTA_MAX_COST: float = 0.0
    # 每用户每窗口的 token 上限(输入+输出)。0 = 不限。
    USAGE_QUOTA_MAX_TOKENS: int = 0

    @property
    def embedding_api_key(self) -> str:
        """实际使用的 Embedding API Key (优先 EMBEDDING_API_KEY,回退 LLM_API_KEY)"""
        return self.EMBEDDING_API_KEY or self.LLM_API_KEY

    @property
    def utility_model(self) -> str:
        """辅助任务(摘要/改写/重排/记忆抽取)实际使用的模型"""
        return self.LLM_UTILITY_MODEL or self.LLM_MODEL

    @property
    def judge_model(self) -> str:
        """裁判实际使用的模型。优先独立裁判模型,其次辅助模型,最后主模型"""
        return self.JUDGE_MODEL or self.LLM_UTILITY_MODEL or self.LLM_MODEL

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
