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


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)  # 文档全文内容
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="indexed")  # indexed, processing, failed
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


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
