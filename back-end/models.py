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
