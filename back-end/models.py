import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    String,
    Text,
    Integer,
    Numeric,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
    Boolean,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    pass


class Workspace(Base):
    """知识库的工作区(组织)。

    知识库的可见单位是工作区,但工作区内部还分两层可见性(见 ``Document.visibility``):

    - ``workspace`` 共享文档:同一工作区全员可见,只有 admin 能增删。
      制度文档是组织资产,不该要求每个员工自己传一遍。
    - ``private`` 私有文档:只有上传者本人可见可删,user 角色也能传。
      临时资料进这里,不污染团队检索。

    加入工作区凭 ``invite_code``。
    """

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(100))
    # 加入凭据。会被人口抄、微信群转发,所以字符表去掉了易混淆字符
    # (见 workspace_service._INVITE_ALPHABET)。泄露后的止损动作是重置,
    # 旧码立即失效。
    invite_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 第三方登录时可为空
    avatar: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # 第三方登录相关字段
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)  # local, github, google
    provider_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 第三方平台的用户ID

    # 所属工作区与角色。workspace_id 为空表示还没初始化(旧用户/OAuth 新用户),
    # 第一次访问工作区相关功能时由 workspace_service.resolve_for_user 自动补建。
    #
    # role 两档,区别只在**共享文档**上:
    #   admin — 可增删工作区共享文档,可重置邀请码
    #   user  — 共享文档只读;但可以自由增删**自己的**私有文档
    # 所以 user 不是"只读账号",它只是不能改组织资产。
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20), default="admin")

    # 账号状态
    is_active: Mapped[bool] = mapped_column(default=True)
    is_verified: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    chats: Mapped[list["Chat"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="chats")
    messages: Mapped[list["Message"]] = relationship(back_populates="chat", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    content: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))  # user, assistant, system
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    chat_id: Mapped[str] = mapped_column(String(36), ForeignKey("chats.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    seq: Mapped[int | None] = mapped_column(Integer, autoincrement=True, unique=True)

    chat: Mapped["Chat"] = relationship(back_populates="messages")


class MessageToolStep(Base):
    """一个回合里的单步工具执行。

    单独建表而不是往 ``messages`` 里塞 role='tool' 的行：那张表是前端逐条渲染的
    对话气泡，多出来的工具行会被当成一条消息画出来，所有读它的地方都得先学会
    过滤。工具轨迹的形状本来也不同（轮次、参数、状态、引用），挤进同一张表
    只能靠一堆可空列。

    不设外键到 messages：触发回合的用户消息会被"编辑并重新生成"删掉，而轨迹是
    可复盘的资产（后续要进 Agent 端到端评估），不该跟着一起消失。失效的轨迹由
    ``revise_user_message`` 按 message_id 显式清理，删对话时按 chat_id 清理。

    ``result_content`` 确实存工具返回的正文，其中可能包含用户自己文档里的内容——
    这与 ``trace_spans.attributes`` 只存元数据的约定不冲突：那是埋点，这是对话
    数据，和 ``messages.content`` 同一性质。
    """

    __tablename__ = "message_tool_steps"
    __table_args__ = (
        Index("ix_message_tool_steps_chat_created", "chat_id", "created_at"),
        Index("ix_message_tool_steps_message", "message_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    chat_id: Mapped[str] = mapped_column(String(36))
    # 触发这一回合的用户消息 id。与 trace_spans.message_id 是同一个锚点，
    # 于是"这次回答走了哪几步"和"这次回答花了多少钱"能对齐到同一轮。
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # 0 表示回合开始前的 RAG 预检索，1 起是模型自己决定的轮次
    round_index: Mapped[int] = mapped_column(Integer, default=0)
    # 同一轮里可能有多个并行工具调用，靠它保持回放顺序稳定
    call_index: Mapped[int] = mapped_column(Integer, default=0)

    tool_name: Mapped[str] = mapped_column(String(120))
    tool_call_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 调用参数的 JSON。这是模型写的，不是用户文本
    arguments: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 哪个子代理执行的这一步。NULL = 主代理自己调的。
    # 不区分的话"这次回答查了 8 次知识库"既可能是主代理反复检索，也可能是一次
    # 委派里 researcher 查了 6 次——这两种情况的改进方向正好相反。
    agent_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 归属的 agent_runs.id。有了它，"这一步是哪次执行做的"是一次 join 而不是
    # 按 (agent_role, 时间) 推断——同一回合里委派两个 researcher 时后者会错。
    # NULL = 这一步产生于 agent_runs 存在之前，或埋点关闭时
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # ToolStatus 的三档（ok / invalid_arguments / unavailable），预检索失败记 error
    status: Mapped[str] = mapped_column(String(20), default="ok")
    # 工具返回的正文，落库时按 TOOL_HISTORY_STORE_MAX_CHARS 截断。
    # 存全量而不是只存摘要：回灌粒度是会反复调的策略，只留摘要等于把当时的
    # 策略烙进数据，回头想换粒度已经没有原始内容可用了。
    result_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 截断前的原始长度，用来判断摘要到底丢了多少
    result_chars: Mapped[int] = mapped_column(Integer, default=0)
    # 命中的引用（document_id / document_name / chunk_index），JSON 数组
    citations: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime)


class AgentRun(Base):
    """一次 Agent 执行的一等记录：主代理一行，每个子代理各一行。

    为什么不继续往 ``message_tool_steps`` 加列：那张表的粒度是"一步工具调用"，
    而这里要回答的问题的粒度是"一次执行"——这次回答起了几个子代理、哪个最慢、
    researcher 失败之后主代理有没有重试、委派在总成本里占多少。用 ``agent_role``
    加排序去推断这些，在一次回答里委派两个同角色子代理时就会失效。

    ``parent_run_id`` 自引用而不设外键：删对话时按 chat_id 显式清理（与
    ``message_tool_steps`` 同一套取舍），外键的级联顺序反而会挡住删除。

    ``status`` 里 ``waiting_approval`` 是唯一一个"没有任何进程在跑它、但它还活着"
    的状态。那正是可恢复执行的意义：一次执行的生命周期不再等于一个 HTTP 请求的
    生命周期。
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_chat_started", "chat_id", "started_at"),
        Index("ix_agent_runs_user_status", "user_id", "status"),
        Index("ix_agent_runs_parent", "parent_run_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    chat_id: Mapped[str] = mapped_column(String(36))
    user_id: Mapped[str] = mapped_column(String(36))
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # NULL = 主代理（与 message_tool_steps.agent_role 同一套约定）
    agent_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # running / waiting_approval / done / failed / abandoned
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    # 走到第几轮。中断恢复后接着涨，不重置
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    # 这次执行里委派了几次（子代理 run 恒为 0，它们不能再委派）
    delegations: Mapped[int] = mapped_column(Integer, default=0)
    # 被人工审批打断过几次。审批一次也没有和审批三次是完全不同的体验，
    # 这个数是"人被打扰了多少次"的唯一记录
    interrupts: Mapped[int] = mapped_column(Integer, default=0)

    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 提示词版本引用（key@version），与 trace_spans.attributes 里的同一个值
    prompt_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # 关联到埋点树，成本与耗时从那边聚合，不在这里重复存
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentCheckpoint(Base):
    """一个 run 在某个安全点上的状态快照。

    一个 run 有多个快照，靠 ``seq`` 排序，最大的那个是当前状态。保留历史而不是
    原地覆盖，是为了"回到第 3 轮再跑一次"——这是评估复现与事后调试要的能力，
    而只留最新快照的话，恢复就只有一个方向。

    ``state`` 是 ``agent_state.TurnState`` 的 JSON。它**确实包含对话消息正文**，
    与 ``message_tool_steps.result_content`` 同一性质（对话数据），
    和 ``trace_spans.attributes`` 只存元数据的约定不冲突——那是埋点。

    体积是这张表的主要代价：一份快照就是整个 messages 列表，一次回答几轮下来
    可能有几十 KB。所以要有保留策略（见 ``checkpoint_store.prune``），
    否则它会比 messages 表本身还大。
    """

    __tablename__ = "agent_checkpoints"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_agent_checkpoints_run_seq"),
        Index("ix_agent_checkpoints_run_seq", "run_id", "seq"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(String(36))
    seq: Mapped[int] = mapped_column(Integer, default=0)

    # pre_tools / waiting_approval / post_tools
    phase: Mapped[str] = mapped_column(String(24), default="pre_tools")
    round_index: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(Text)
    # 中断请求的 JSON，仅 waiting_approval 的快照有
    interrupt: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)  # 文档全文内容
    # 知识库的外层作用域:工作区。检索/去重/缓存都先按它过滤
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 上传者。**参与权限判断**:private 文档只有 user_id == 当前用户时可见可删。
    # 改动之前这一列只用于展示"这份文档是谁放的",加了 visibility 之后它成了
    # 私有文档的归属键——所以它为 NULL 的 private 文档谁都看不见(见下)。
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 可见性:workspace = 工作区共享(仅 admin 可增删),private = 仅上传者可见。
    #
    # 默认 workspace 而不是 private,理由是**存量数据**:这一列加上去之前所有文档
    # 都是工作区共享语义,迁移时必须保持原样,否则升级一次就等于把团队知识库
    # 全部变成某个人的私有文档。新上传的默认值由调用方给,不靠这里
    # (chat 附件默认 private,知识库页面上传默认 workspace)。
    #
    # user_id 为 NULL 且 visibility=private 的组合是**不该长期存在的中间态**:
    # 那种文档谁都检索不到、谁都删不掉,是一份没人能处置的孤儿。它由
    # ondelete="SET NULL" 在删用户时造出来。
    #
    # 2026-08-25 起启动时会把它们收编成工作区共享文档
    # (workspace_service.adopt_orphaned_documents),让 admin 能看见并自己决定
    # 删还是留。此前的注释写的是相反的语义("需要 admin 显式处理"),但那件事
    # 当时**没有任何接口能做**——admin 连列表都看不到它们,所谓"显式处理"
    # 实际等于永久滞留。
    #
    # 收编后 user_id 保持 NULL,那正是"原上传者已离开"的标记:共享文档正常
    # 都带 user_id,所以 (visibility=workspace, user_id IS NULL) 这个组合能
    # 零成本地把继承来的文档挑出来,不需要额外加列。
    visibility: Mapped[str] = mapped_column(String(16), default="workspace", index=True)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="indexed")  # indexed, processing, failed
    # 解析后正文的 sha256。去重键:同一内容传两遍会占两套 chunk,RRF 按不同
    # chunk_id 融合不会合并,重复文档会挤掉 top_k 里的其他文档。
    # 哈希算在解析后的文本而不是原始字节上——同一份内容换个文件名、或
    # PDF 重新导出一次,应该被认出是同一篇文档。
    #
    # 去重范围是(工作区, 可见性作用域, 哈希),其中"可见性作用域"对共享文档是
    # 整个工作区、对私有文档是上传者本人。加 visibility 之前它只是(工作区, 哈希),
    # 那会让两个人各自上传同一份文件时后一个人拿到**前一个人的私有文档**——
    # 既是越权也是错误的复用。反过来,同一个人把自己的私有文档再传一遍仍然去重。
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 解析后端(text/utf-8、text/gb18030、pdfplumber、pypdf2……)与解析告警。
    # 这条链路上最常见的失败全都不抛异常:扫描件抽出空文本、GBK 解成一串替换符、
    # 没识别出标题层级。改动前它们一律落成 indexed,界面上和正常文档毫无区别,
    # 只是永远检索不到。这两列就是"为什么这篇文档不对"的唯一记录。
    parse_backend: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parse_warnings: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 数组
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class UserMemory(Base):
    """跨会话的长期记忆:用户的事实与偏好。

    与滚动摘要的分工:摘要活在单个会话内,压的是"这段对话说过什么";
    这里存的是"关于这个用户,哪些信息值得在**所有**以后的对话里知道"
    ——部门、角色、偏好、长期约束。抽取由辅助模型在每轮回答后异步完成,
    注入发生在系统提示词之后、历史之前。
    """

    __tablename__ = "user_memories"
    __table_args__ = (
        Index("ix_user_memories_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String(36))
    # fact(已确认的事实) / preference(表达的偏好)。kind 只做展示分组,
    # 注入时不加区分——对模型来说都是"关于用户的背景"。
    kind: Mapped[str] = mapped_column(String(20), default="fact")
    content: Mapped[str] = mapped_column(Text)
    # 这条记忆来自哪个会话,便于用户追问"你为什么知道这个"时溯源
    chat_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 序列化的向量
    chunk_index: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Prompt(Base):
    __tablename__ = "prompts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(100), default="General")
    content: Mapped[str] = mapped_column(Text)  # 提示词模板正文（含 {input} 占位）
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_public: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TraceSpan(Base):
    """一次回答里的单个执行片段（模型调用 / 工具执行 / 检索 / 向量化）。

    单表存全部 kind，靠 parent_id 组成树。这和 OpenTelemetry 的做法一致：
    与其为每类操作建一张表，不如让共有字段（耗时、状态）成为列、
    差异字段落在 attributes JSON 里——查询时才不用 union 五张表。

    不设外键到 chats/messages：埋点不该阻止业务数据被删除，
    也不该因为级联删除而丢掉历史成本记录。
    """

    __tablename__ = "trace_spans"
    __table_args__ = (
        Index("ix_trace_spans_trace_started", "trace_id", "started_at"),
        Index("ix_trace_spans_user_started", "user_id", "started_at"),
        Index("ix_trace_spans_chat_started", "chat_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))

    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 触发这次回答的用户消息 id，用于把一棵 trace 关联回具体对话轮次
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)

    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # prompt_tokens 中被提供商上下文缓存命中的部分。是 prompt_tokens 的子集，
    # 不是额外的量——聚合时不能和它相加。NULL 表示这次调用没有缓存信息
    # （提供商没回传，或 token 数是本地估算的），与"命中 0 个"含义不同。
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # provider(提供商回传) 或 estimated(本地估算)，聚合成本时必须能区分
    token_source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # 价目表未配置该模型时为 NULL —— 表示"未知"，不是"零成本"。
    # 用 Numeric 而非 float：成本要累加，二进制浮点的误差会一路攒下去。
    cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # 仅元数据（轮次、候选数、命中通道等），不存提示词与用户文本
    attributes: Mapped[str | None] = mapped_column(Text, nullable=True)


class MessageFeedback(Base):
    """用户对某条助手回答的评价。

    一条消息只保留一份反馈（``message_id`` 唯一）：用户改主意时更新而不是追加，
    否则"点了三次踩"会被算成三个负样本，把满意度指标压得比实际更低。

    不设外键到 messages：这一行是**自洽**的——question / model / expected_answer
    都存在本行里，不依赖那条消息还在。所以"编辑并重新生成"删掉旧回答之后，
    它依然是一条有效的回归用例，不该被级联删除带走。

    用户显式删除整个对话是另一回事，那时反馈会跟着删（取舍写在
    ``feedback_service.discard_chat``）。删除策略由代码决定而不是数据库默认，
    正是不设外键的目的。
    """

    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_message_feedback_message"),
        Index("ix_message_feedback_user_created", "user_id", "created_at"),
        Index("ix_message_feedback_rating", "rating", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    message_id: Mapped[str] = mapped_column(String(36))
    chat_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)

    # up / down。只做两档：五星量表在单人使用场景里分辨不出差别,
    # 反而让"到底几星算差"变成新的争论点。
    rating: Mapped[str] = mapped_column(String(8))
    # 差评原因标签(不准确/没引用/答非所问/格式差/其它),便于按类型聚合
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 用户补充说明与期望答案。会随反馈导出进评估集,所以这里**确实**存用户文本——
    # 与 trace_spans.attributes 的约定不同,那是埋点,这是用户主动提交的标注。
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 反馈时的模型与提问,导出回归用例时需要,不必再回表拼
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    # 是否已被导出进评估数据集,避免每次导出都重复追加同一条
    exported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
