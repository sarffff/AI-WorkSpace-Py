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
    # 不设外键：0007 建列时就没带，库里至今也没有，而应用层从不删工作区——
    # 那句 ondelete="SET NULL" 是一次都不会兑现的空头承诺。留着它反而让
    # create_all 建出来的测试库（SQLite 会建 FK）与生产库结构不一致。
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="admin")

    # 账号状态
    is_active: Mapped[bool] = mapped_column(default=True)
    is_verified: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)  # 文档全文内容
    # 知识库的外层作用域:工作区。检索/去重/缓存都先按它过滤
    # 不设外键：0007 建列时就没带，库里至今也没有，而应用层从不删工作区——
    # 那句 ondelete="SET NULL" 是一次都不会兑现的空头承诺。留着它反而让
    # create_all 建出来的测试库（SQLite 会建 FK）与生产库结构不一致。
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 上传者。**参与权限判断**:private 文档只有 user_id == 当前用户时可见可删。
    # 改动之前这一列只用于展示"这份文档是谁放的",加了 visibility 之后它成了
    # 私有文档的归属键——所以它为 NULL 的 private 文档谁都看不见(见下)。
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # 可见性:workspace = 工作区共享(仅 admin 可增删),private = 仅上传者可见。
    #
    # 默认 workspace 而不是 private,理由是**存量数据**:这一列加上去之前所有文档
    # 都是工作区共享语义,迁移时必须保持原样,否则升级一次就等于把团队知识库
    # 全部变成某个人的私有文档。新上传的默认值由调用方给,不靠这里
    # (个人上传默认 private，知识库页面上传默认 workspace)。
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


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("documents.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 序列化的向量
    chunk_index: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TraceSpan(Base):
    """一次回答里的单个执行片段（模型调用 / 工具执行 / 检索 / 向量化）。

    单表存全部 kind，靠 parent_id 组成树。这和 OpenTelemetry 的做法一致：
    与其为每类操作建一张表，不如让共有字段（耗时、状态）成为列、
    差异字段落在 attributes JSON 里——查询时才不用 union 五张表。

    不设外键到 tickets：埋点不该阻止业务数据被删除，
    也不该因为级联删除而丢掉历史成本记录。
    """

    __tablename__ = "trace_spans"
    __table_args__ = (
        Index("ix_trace_spans_trace_started", "trace_id", "started_at"),
        Index("ix_trace_spans_user_started", "user_id", "started_at"),
        Index("ix_trace_spans_ticket_started", "ticket_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))

    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 一次编排运行的归属：工单 id。NULL = 不属于任何工单（健康巡检、后台索引）
    ticket_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

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


class WorkspaceSkill(Base):
    """一个工作区自己写的 skill（作业指导）。

    skill 回答"这件事在本组织该怎么做"。内置 skill 在 ``back-end/skills/`` 里跟
    代码版本化，这张表装的是各家自己的 SOP——admin 在界面上写，不改代码不重启。

    ## 判据是 workspace_id

    SOP 是**组织资产**（全公司同一套退款流程）。按 user 存的话每个坐席都要自己
    录一遍，而且会录出互相矛盾的版本——那时"到底按谁的流程办"没有答案。

    ## 同名盖掉内置

    ``(workspace_id, name)`` 唯一，查找时工作区优先。不做"两份正文合并"：
    拼在一起时哪一句生效取决于模型，而那不可预测。

    ## description 是模型选 skill 的唯一依据

    索引里只有名字和这一句（正文按需用 ``load_skill`` 取），所以它写不好就等于
    这个 skill 不存在——不会报错，只是永远不被选中。
    """

    __tablename__ = "workspace_skills"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_workspace_skills_ws_name"),
        Index("ix_workspace_skills_workspace_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    # 关掉而不是删掉：改坏一条 SOP 之后想先停用看看，比删了重录便宜
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 正文或描述变化时 +1（在 service 层判定）。审核结论要引用它，否则 admin 改一次
    # SOP，之前所有结论的依据就都指向一份已经不存在的文本——三个月后有人问"当时
    # 为什么通过"，答不出来。enabled 开关和改错别字不让它跳，否则这个号很快大到
    # 没人看，"版本变了"这个信号也就没用了。
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 下结论之前必须先拿到的东西，逗号分隔。解析走 skill_library.parse_required_inputs
    # （和内置 skill 的 frontmatter 共用一套，各写一遍迟早在"逗号后空格算不算"上分叉）。
    #
    # 它不是给模型看的提示，是给 structured.ReviewVerdict 提供必填槽位：结构里有
    # 那个位置，空着就是空着。于是"漏了一项"从判断题变成填空题。
    required_inputs: Mapped[str] = mapped_column(
        String(500), default="", nullable=False
    )
    # SOP 出问题时第一个要问的就是这个
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    """防篡改审计链：谁、什么时候、做了哪一个改变状态的动作。

    与已有两张"留痕"表互补，不替代：``CsOperation`` 记一次业务写操作的详细状态
    （幂等键、批没批、执行结果）；``TraceSpan`` 是可观测性埋点，刻意不存正文、按窗口聚合。
    这张表回答合规要问的另一个问题——"这串动作有没有被事后改过"。

    手段是**哈希链**：每条存上一条的 ``entry_hash`` 作 ``prev_hash``，自己的
    ``entry_hash`` 覆盖 (prev_hash + 关键字段)。改动或删除中间任意一条，它之后每一条
    的链接都对不上，``audit_log.verify`` 一走就能定位到断点。

    **链按 actor 分段，不做全局单链。** 全项目没有 admin/role，所有读接口都按
    ``user_id`` 自作用域。按用户分段的链正好落进这个模型：用户能 verify 自己那条、
    看不到别人的；全局单链在没有管理员时既没法自洽地读、又要扛并发追加的分叉。

    ``seq`` 是**该 actor 内**的序号（record 时取其当前最大值 +1），不是数据库自增
    主键——避开 BIGINT 自增在 SQLite/MySQL 间的可移植坑，也让"这是第几条"与其他
    用户的写入无关。
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        # 链遍历与自作用域读取都按 (actor, seq)
        Index("ix_audit_log_actor_seq", "actor_id", "seq"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 该 actor 链内的序号，从 1 起。并发同 actor 追加极少见，真撞上会让链在
    # verify 时报断点（而非静默改数）——可接受的诚实边界
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 复合索引 ix_audit_log_actor_seq 已覆盖 actor 前缀查询，这里不再单列 index
    actor_id: Mapped[str] = mapped_column(String(36))

    # tool.write / approval.decision / auth.login / auth.register …
    action: Mapped[str] = mapped_column(String(40))
    # 动作作用于什么：工具名、文档 id、或一句短描述
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 参数摘要与预览，复用 approval_audit 的算法（sorted-json sha256 + 截断）。
    # 不存完整参数：写知识库正文可到 AGENT_WRITE_MAX_CHARS，digest 足以证明同一性
    args_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    args_preview: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 线索，非外键：审计不该阻止业务数据被删。NULL = 这次动作不属于任何工单
    # （登录、改文档、暂停 Agent）
    ticket_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # 链：prev_hash = 上一条的 entry_hash（首条为 genesis 常量）
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))

    # 在 record() 里用 naive_now() 显式落值——它进哈希载荷，必须在算 hash 时就已知，
    # 不能交给数据库 server_default
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Notification(Base):
    """发给某个用户的"有事等你处理"通知。

    为什么要有它：工单挂在人工审批上、或到点转人工时，这件事**只在那条实时
    SSE 连接上存在一瞬**——刷新、切页、断网之后就没了，审批人只能靠打开队列
    重新发现。人在回路是这个产品的核心，审批人不在场时闭环就断在这里。这张表
    把"有事等你"落成持久的 per-user 收件箱，与那条易失连接解耦。

    不设外键：``ticket_id`` 只作深链线索（点通知跳回那张工单），断了不影响通知
    本身——同 TraceSpan 的取舍。全局健康告警没有对应工单，那行为 NULL。

    ``read_at`` 为 NULL 即未读。本轮只做应用内拉取式收件箱；给客户的回复走
    ``ticket_outbox``（那是面向客户的出口，不是坐席的通知）。
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # 收件箱按时间倒序翻页
        Index("ix_notifications_user_created", "user_id", "created_at"),
        # 未读计数 / 未读筛选
        Index("ix_notifications_user_read", "user_id", "read_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 收件人：要处理这件事的那个人（审批人 / 发起人）
    user_id: Mapped[str] = mapped_column(String(36))
    # approval_required / ticket_handoff / health_alert
    # （前两条是 per-ticket 的人审与交接事件，带 ticket_id；health_alert 是线上
    #  健康监控的全局告警，发给管理员、不带 ticket_id，去重靠"每人同时只留一条未读"）
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 深链线索，非外键
    ticket_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # NULL = 未读
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DocumentJob(Base):
    """一篇文档的持久化索引任务。

    改动前入库索引走 FastAPI ``BackgroundTasks``：进程内、不落盘、无重试、无并发上限，
    进程在索引中途重启那篇就永远卡在 ``processing``。这张表把"要索引谁"落成持久状态，
    配合 ``services/document_queue.py`` 的认领/租约/重试，形态与 ``ticket_outbox`` 的
    lease/reaper 完全一致（都挂在读路径上惰性清理，项目里没有调度器）。

    不设外键到 documents：删文档时孤儿任务无害——``index_document`` 对"文档已消失"
    本就优雅返回（见其首行判断），下一次认领时自然 complete 掉。与 ``trace_spans`` /
    ``notifications`` 同一套"side-table 不设 FK、删除顺序由代码控制"的取舍。

    ``available_at`` 是退避的载体：重试时把它推到未来，``claim_next`` 只认领
    ``available_at <= now`` 的任务。``attempts`` 在**认领时**自增（而非失败时），
    这样租约过期被回收的任务也会消耗一次尝试，不会无限重试一个每次都把进程拖垮的任务。
    """

    __tablename__ = "document_jobs"
    __table_args__ = (
        # 认领扫描：按 (status, available_at) 取最老的可认领任务
        Index("ix_document_jobs_status_available", "status", "available_at"),
        # 入队去重 / 按文档查任务
        Index("ix_document_jobs_document", "document_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 索引由下面 __table_args__ 的 ix_document_jobs_document 提供；这里再写
    # index=True 只会多出一个同名同列的自动索引（..._document_id）。
    document_id: Mapped[str] = mapped_column(String(36))
    # queued / running / succeeded / failed
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    # 0-100 的粗粒度进度（queued=0 / running=10 / done=100）。细粒度要在
    # index_document 里埋钩子，不值当——入库对用户是"传完等一会儿"，粗粒度够用。
    progress: Mapped[int] = mapped_column(Integer, default=0)
    # 认领者标识与租约到期时刻。NULL = 当前没有进程持有它。
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 最早可认领时刻（退避把它推向未来）
    available_at: Mapped[datetime] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# 工单域（客服 + 工单解决 Agent）
#
# 下面两组表服务两件不同的事，别混：
#   1. ``tickets`` / ``ticket_events`` / ``cs_governors`` 是**这个 Agent 自己的**
#      工单生命周期、可回放轨迹与治理开关。
#   2. ``cs_*`` 里其余的表是**外部业务系统的靶子**（订单、物流、退款、发票）。
#      真实部署里它们是 ERP/CRM/支付网关，本 Agent 只能通过工具读它们；这里自建
#      同名结构是为了让写操作（改地址、发起退款）有对象可写、能在离线评估里
#      断言"到底改没改对"。它们由 ``services/ticket/`` 下的工具独占访问，
#      业务代码不要绕过工具直接写。
#
# 状态列一律 ``String(n)`` 而不是枚举类型（同 ``Document.status``：MySQL 改
# 枚举要锁表，而这些取值集还会长）。时间列一律由应用侧 ``naive_now()`` 显式写入
# （同 ``DocumentJob``/``Notification``），不要交给 ``server_default``——一个进程内
# 混两个时区，回放出来的顺序就是错的。
# ---------------------------------------------------------------------------


class Ticket(Base):
    """一张工单：Agent 的工作单元，也是所有指标的计数单位。

    为什么不复用会话表：对话是**逐条气泡渲染**的前端模型，一张工单要挂的是
    客户、订单、风险等级、SLA、CSAT、处置结论，且它的生命周期**长于一次会话**
    （高风险操作等人批可能要几小时，跨天恢复靠的是它自己的状态而不是聊天记录）。

    ``risk_level`` 可空而 ``status`` 不可空：NULL 读作"还没评估过"，和"评估完判定
    为低风险"是两回事——把后者当成前者会让没走过风险节点的工单悄悄走自动执行。
    """

    __tablename__ = "tickets"
    __table_args__ = (
        # 待办队列：按工作区筛状态、按时间排。坐席打开工单台看到的就是这个顺序
        Index("ix_tickets_ws_status_created", "workspace_id", "status", "created_at"),
        # 指标聚合：自动解决率、平均处理时长都是"某窗口内的工单"
        Index("ix_tickets_ws_created", "workspace_id", "created_at"),
        Index("ix_tickets_customer", "customer_id"),
        # 渠道重投递去重。external_ref 为空时不参与（MySQL/SQLite 的唯一索引都
        # 放行多个 NULL），所以网页聊天这类没有外部 ID 的渠道照常工作
        UniqueConstraint(
            "workspace_id", "channel", "external_ref", name="uq_tickets_ws_channel_extref"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 工单是组织资产（同 Document 的共享语义），归属挂 workspace
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 客户档案。NULL = 还没认出是谁（匿名邮件、只有订单号）——认出来之后由工具回填
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 人类受理人。NULL = 无人认领，Agent 自己在处理
    assignee_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # web_chat / email / app / wecom / phone / api
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # 渠道侧的消息或邮件标识。有了它，同一封邮件被重复投递只会建出一张工单
    external_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # new / understanding / planning / acting / awaiting_approval / escalated /
    # resolved / closed / failed
    status: Mapped[str] = mapped_column(String(24), default="new", nullable=False)
    # low / mid / high。NULL = 尚未评估（见类文档）
    risk_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # 意图识别的结果。自由文本而不是枚举：诉求类型清单会随业务变
    intent: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 实体抽取结果（订单号、商品、金额、诉求类型、情绪）。整体读写，不拆表
    entities: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 客户原话。Text：工单正文长度天然不可预知，给上限就一定有被截尾的那天，
    # 而被截掉的尾部往往正是订单号和诉求
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 附件（文件名或 URI）列表，json 数组
    attachments: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 一句话把这张工单说清楚，供队列与审批列表显示
    summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # 处置结果。resolved_* 与 status 分开：状态说明走到哪一步了，处置说明最后
    # 到底怎么解决的（退款/改地址/仅答复/转人工），指标要的是后者
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # 转人工的原因，用于反查"该转的没转"这类错误
    escalation_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # 客户满意度评分（1-5）与留言。NULL = 客户没评，不要拿 0 当"没评"
    csat_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    csat_comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # 成本与效率：这两个数是"自动解决率/平均处理时长"的原始材料，也是单工单
    # 预算熔断的判定输入。计数与金额都由编排层在每步之后累加，不靠事后重算轨迹
    tool_rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)

    # 首次响应时间（对外给出第一条答复）与 SLA 到期时刻。
    # 两者都为空是正常态：前者要等真回复了才写，后者只有配了 SLA 才写
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TicketEvent(Base):
    """工单的一步轨迹：append-only，用于事后回放与审计。

    文档要求的"每一步思考、工具调用、结果都可回放"落在这里。与 ``trace_spans``
    分工明确、互不替代：埋点是**可观测性**那一层（刻意不存正文、按窗口聚合、会被
    清理），而这张表是**业务留痕**——工单是从业务视角被翻出来看的对象，"当时为什么
    这么决定"必须在埋点被清掉之后仍然查得到。所以 ticket_id 不设外键。

    存的是**摘要**不是全文：正文由 trace_spans 之外的原始工单文本承担，这里存到能
    看懂决策为止（参数走 digest + preview，复用 approval_audit 的算法）。

    ``seq`` 是该工单内的序号，应用侧取当前最大值 +1（同 ``AuditLog.seq`` 的取舍：
    避开自增主键在 SQLite/MySQL 间的可移植坑）。
    """

    __tablename__ = "ticket_events"
    __table_args__ = (
        Index("ix_ticket_events_ticket_seq", "ticket_id", "seq"),
        Index("ix_ticket_events_ws_created", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    ticket_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # 状态机节点：intake / understand / risk / retrieve / plan / act / confirm / escalate
    node: Mapped[str] = mapped_column(String(24), nullable=False)
    # thinking / tool_call / tool_result / approval / decision / state_change /
    # error / reply
    kind: Mapped[str] = mapped_column(String(24), nullable=False)

    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 与 AuditLog 同一套摘要策略：digest 证明同一性，preview 供人读
    args_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    args_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ok / error / blocked / rejected / pending
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 该步的文本：思考摘要、给客户的答复、转人工说明
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    round_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CsGovernor(Base):
    """一个工作区的治理开关。一行，按 workspace 唯一。

    为什么不用 ``config``：``TICKET_*`` 环境变量要重启才生效，而"一键全局暂停"
    是在事故当中按下的——那一刻没有部署窗口。所以限额与暂停态必须是**运行时可改**
    且改完留痕的。config 里的同名设置退化为新工作区的默认值。

    这里**不存**当日退款已用额度。那是一个可以从 ``cs_operations`` 聚合出来的
    事实，存下来就有一个"计数与实际不一致"的窗口（并发写入、进程崩溃、事后补记），
    而治理限值是安全边界，宁可每次多算一次查询也不能读到脏数字——同
    ``usage_guard`` 走"读路径顺带聚合"而不维护计数器的做法。

    限额列可空：NULL 表示"沿用全局默认"，与 0（一律不许退）分得开。
    """

    __tablename__ = "cs_governors"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_cs_governors_workspace"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)

    paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pause_updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # 当日退款总额上限（按应用时区自然日聚合）
    daily_refund_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # 单工单成本上限与工具调用次数上限
    max_cost_per_ticket: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    per_ticket_tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CsCustomer(Base):
    """客户档案：跨工单的长期画像载体。

    一个客户在网页聊天里是 session id、在邮件里是一个地址、在企微里是 external
    userid。``email`` / ``phone`` 存**归一化之后**的形式（小写、只留数字），
    否则同一个人会因为写了 ``+86`` 而被当成两个人，画像与限额都会分裂。
    归一化只在 ``services/ticket/intake.py`` 做一次，别处不要各自再削一遍。

    ``profile`` 是随时间累积的结论（偏好、历史问题摘要、沟通禁忌），整体读写；
    ``profile_version`` 在每次改写时自增，让"这条工单用的是第几版画像"可以被
    轨迹记下来——画像会变，而事后复盘要的是当时看到的那一版。
    """

    __tablename__ = "cs_customers"
    __table_args__ = (
        Index("ix_cs_customers_ws_email", "workspace_id", "email"),
        Index("ix_cs_customers_ws_phone", "workspace_id", "phone"),
        Index("ix_cs_customers_ws_external", "workspace_id", "external_ref"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 渠道侧的客户标识（企微 external userid / APP userId）。线索，不做唯一约束：
    # 同一人可以从两个渠道进来，那正是要靠画像合并而不是分裂成两行的场景
    external_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # standard / vip / blacklist 之类。自由文本，档位清单是运营的事
    tier: Mapped[str] = mapped_column(String(20), default="standard", nullable=False)
    lifetime_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), nullable=False
    )
    # json 数组：投诉史、法律函件、多次退款等标记。风险分级要读它，所以单独成列
    # 而不是埋进 profile——后者是给模型看的散文
    risk_flags: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 长期用户画像（偏好、历史问题、沟通注意点），json 对象
    profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CsOrder(Base):
    """订单头。地址直接放在订单上而不是另立一张表：本系统里地址是**订单的快照**
    （发货之后就固定了），"改地址"改的是这张还没发货的订单的快照，不是客户的通讯录。

    ``refunded_amount`` 单独一列是退款幂等的第二道保险：即便某个重复请求带着新的
    幂等键绕过了 ``cs_operations`` 的唯一约束，"累计退款 > 可退金额"这条校验
    仍然会挡住超额。
    """

    __tablename__ = "cs_orders"
    __table_args__ = (
        UniqueConstraint("workspace_id", "order_no", name="uq_cs_orders_ws_orderno"),
        Index("ix_cs_orders_customer", "customer_id"),
        Index("ix_cs_orders_ws_created", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 客户口中报出来的那个号。工具按它查，所以它必须在工作区内唯一
    order_no: Mapped[str] = mapped_column(String(40), nullable=False)
    # pending_payment / paid / packed / shipped / delivered / cancelled /
    # refunding / refunded
    status: Mapped[str] = mapped_column(String(20), default="paid", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), nullable=False
    )
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), nullable=False
    )

    # 收货信息。改地址写的就是这三列
    receiver_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    receiver_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address_text: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # 商品与库存扣减是"下单"那一刻的既成事实，之后的取消只改状态不改数量，
    # 所以这里记快照而不引库存表
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CsOrderItem(Base):
    """订单行。实体抽取里"商品"这一项要有东西可对，退款也要能按行退。"""

    __tablename__ = "cs_order_items"
    __table_args__ = (Index("ix_cs_order_items_order", "order_id"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), nullable=False
    )
    # 行级状态可与订单头不同（部分发货）。NULL = 跟随订单头
    line_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CsShipment(Base):
    """物流。查物流是低风险高频操作，所以它的轨迹量最大，值得单独一张表。"""

    __tablename__ = "cs_shipments"
    __table_args__ = (
        Index("ix_cs_shipments_order", "order_id"),
        Index("ix_cs_shipments_tracking", "tracking_no"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    carrier: Mapped[str] = mapped_column(String(40), nullable=False)
    tracking_no: Mapped[str] = mapped_column(String(64), nullable=False)
    # pending / in_transit / out_for_delivery / delivered / exception / returned
    status: Mapped[str] = mapped_column(String(20), default="in_transit", nullable=False)
    # 最后一条轨迹描述。真实系统里是一长串节点，这里留最后一条 + 时间够用
    last_event: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CsRefund(Base):
    """退款申请单。资金类操作，全程要能被审计出来。

    ``idempotency_key`` 上压了工作区内唯一约束——幂等在这里是**数据库事实**而不是
    应用约定。模型重试、进程崩溃后恢复、用户重复点击，走的都是同一个键，撞了唯一
    约束就返回首次结果，而不是再退一笔。这一条是文档里"每个写操作必须带幂等键"
    唯一的硬保证。

    这张表**只记状态，不动钱**。真实部署里 ``executed`` 由支付网关回调写入；
    这里由工具写，评估集断言的是"该不该执行、金额对不对"。
    """

    __tablename__ = "cs_refunds"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_cs_refunds_ws_idem"),
        Index("ix_cs_refunds_order", "order_id"),
        Index("ix_cs_refunds_customer", "customer_id"),
        # 当日退款额聚合：按 (workspace, status, created_at) 扫
        Index("ix_cs_refunds_ws_status_created", "workspace_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 是哪张工单要退的。线索，非外键
    ticket_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    # requested / awaiting_approval / approved / executed / rejected / failed
    status: Mapped[str] = mapped_column(String(20), default="requested", nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 人审痕迹：谁批的、批的还是改过的。这张表是业务侧的台账——"这笔退款谁批准的"
    # 必须能在删掉工单、清掉轨迹之后仍然回答
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CsInvoice(Base):
    """发票申请。低风险写操作的代表（文档 Phase 2），所以它要有自己的表，
    而不是和退款挤在一起——两者的权限档位完全不同。"""

    __tablename__ = "cs_invoices"
    __table_args__ = (
        Index("ix_cs_invoices_order", "order_id"),
        Index("ix_cs_invoices_ws_status", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ticket_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # 发票抬头与税号。税号可空：个人抬头的发票就没有它
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    tax_no: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # requested / issued / rejected。issued 由财务系统回填，本仓库的工具只到 requested
    status: Mapped[str] = mapped_column(String(16), default="requested", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CsOperation(Base):
    """业务系统写操作的账本：幂等的落点、限额的聚合源、审计的原始材料。

    这张表回答三个各自都需要独立答案的问题：
      - **做过没有**（幂等）：``idempotency_key`` 上有唯一约束，撞了就返回首次结果，
        状态记成 ``replayed`` 而不是再执行一遍。工具层的幂等检查之所以能跨进程、
        跨重启，靠的是这行记录，不是内存字典。
      - **今天用了多少**（预算）：``amount`` 按自然日聚合就是当日退款额度。
      - **是谁、按什么参数做的**（审计）：digest + preview 与 ``AuditLog`` 同一套
        摘要策略，但这里是业务视角（工具名 + 目标 + 结果），``AuditLog`` 是防篡改链。

    ``status`` 里 ``blocked`` 是治理拦截（限额、暂停、权限不足），``pending_approval``
    是人审挂起——两者都必须留痕，否则"Agent 想退但被拦了"和"Agent 压根没提"在
    复盘时看不出来，而这两个恰恰是不同的故障。
    """

    __tablename__ = "cs_operations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_cs_operations_ws_idem"),
        Index("ix_cs_operations_ticket", "ticket_id"),
        Index("ix_cs_operations_ws_created", "workspace_id", "created_at"),
        # 当日资金额度聚合
        Index("ix_cs_operations_ws_kind_time", "workspace_id", "permission", "created_at"),
        # 错误操作率的聚合：窗口内已复盘的执行操作里有多少被判 wrong
        Index("ix_cs_operations_ws_verdict", "workspace_id", "verdict", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ticket_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # order.update_address / refund.create / order.cancel / invoice.request …
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    # read / mutate / fund —— 权限档位记在账上，事后才能证明"这一笔当时是高权限"
    permission: Mapped[str] = mapped_column(String(8), nullable=False)

    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    args_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    args_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # executed / replayed / blocked / failed / pending_approval
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 资金类操作的金额，用于当日额度聚合。非资金类为 NULL（不是 0——0 会被算进额度）
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ---- 人工复盘：这一笔到底办对没有 ----
    #
    # 文档把"错误操作率（尤其是退款、改订单）"列为核心指标，而它的定义是
    # **"执行了，但不该执行"**——这个数没法从系统内部算出来。运行时的每一道检查
    # 只能证明"这一笔通过了当时的权限档位、阈值、余额与幂等"；"当时就不该退"
    # 是人对业务后果的判断。没有下面这几列，能给的只有被拒率和被拦率这两个代理值
    # ——``/tickets/metrics`` 里那个 ``rejectedAttemptRate`` 之所以不敢叫错误操作率，
    # 就是这个原因。
    #
    # 只有真的执行过的才值得复盘：给一次被拦下的操作打"错了"会污染分母，
    # 让率值忽高忽低却什么都没说。
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # ok / wrong。NULL = 还没人看过。不用枚举类型，同本文件开头那条约定
    verdict: Mapped[str | None] = mapped_column(String(8), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 判成 wrong 之后人实际怎么补救的（refunded_less / contacted_customer …）。
    # 档位是自由文本：补救动作的清单归运营，而这一列存在的理由是
    # "错了之后做了什么"比"错了"本身更能指导下一步该收紧哪条阈值。
    corrected_action: Mapped[str | None] = mapped_column(String(40), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TicketOutbox(Base):
    """要发给客户的东西：回话、邀评。持久化的发送队列。

    为什么必须有这张表而不是在办结那一刻直接发：文档§2 的闭环是"生成回复 →
    更新 CRM → 关闭工单"，而现在它实际是"生成回复 → 写进库"。中间那一步需要
    一个能失败、能重试、能被看见的地方。直接发会带来两个具体的坏结果——

    1. 发送失败就没人再发。工单已经标成 resolved 了，客户却从来没收到过回话，
       而所有指标都显示这一单办得又快又好。
    2. 一次外发失败会把已经办完的工单整个回滚，那件事的后果比"晚一点再发"重得多。

    所以状态在库里，发送是幂等的重试（形态照搬 ``document_jobs`` 的
    lease/reaper：认领时自增 attempts、租约过期会被别人捡走）。

    ``suppressed`` 是一个**必须有人明确选择**的状态（渠道没接通、客户拒收），
    不是"发不出去就标一下"的默认归宿——被抑制的消息不会自己再发出去，
    写错这一状态就等于替客户决定了"不用回他"。

    没有接通道时 ``deliver`` 什么都不做、也不改状态：那些行留在 pending 里，
    是"欠客户一个回复"的可见证据。把 pending 悄悄变成 sent 是这一层最坏的一种
    撒谎，所以宁可队列长。
    """

    __tablename__ = "ticket_outbox"
    __table_args__ = (
        # 认领扫描：按 (status, available_at) 取最老的待发送
        Index("ix_ticket_outbox_status_available", "status", "available_at"),
        Index("ix_ticket_outbox_ticket", "ticket_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # 线索，非外键：工单被清理不该带走一条没发出去的话
    ticket_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # reply（给客户的回话）/ csat_invite（邀评）
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # 回投到工单原来的渠道。发不出去的时候要知道"该走哪条路"
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # 归一化之后的收件人（邮箱小写、电话只留数字）。NULL = 渠道没给可寻址的收件人
    recipient: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # pending / sending / sent / failed / suppressed
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # 最早可发送时刻（退避的载体）
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
