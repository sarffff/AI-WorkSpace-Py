"""新增 trace_spans：模型调用/工具/检索的埋点

Revision ID: 0002_trace_spans
Revises: 0001_baseline
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_trace_spans"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trace_spans",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("parent_id", sa.String(32), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        # 刻意不加外键：埋点不该阻止业务数据被删除，
        # 也不该因为级联删除而丢掉已经发生过的成本记录
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("chat_id", sa.String(36), nullable=True),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("error_type", sa.String(80), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("token_source", sa.String(16), nullable=True),
        # NULL 表示"价目表里没有这个模型"，即成本未知，而不是零成本
        sa.Column("cost", sa.Numeric(14, 6), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("attributes", sa.Text(), nullable=True),
    )
    op.create_index("ix_trace_spans_trace_id", "trace_spans", ["trace_id"])
    op.create_index(
        "ix_trace_spans_trace_started", "trace_spans", ["trace_id", "started_at"]
    )
    op.create_index(
        "ix_trace_spans_user_started", "trace_spans", ["user_id", "started_at"]
    )
    op.create_index(
        "ix_trace_spans_chat_started", "trace_spans", ["chat_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_trace_spans_chat_started", table_name="trace_spans")
    op.drop_index("ix_trace_spans_user_started", table_name="trace_spans")
    op.drop_index("ix_trace_spans_trace_started", table_name="trace_spans")
    op.drop_index("ix_trace_spans_trace_id", table_name="trace_spans")
    op.drop_table("trace_spans")
