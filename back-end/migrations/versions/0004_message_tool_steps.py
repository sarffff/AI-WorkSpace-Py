"""新增 message_tool_steps：Agent 工具轨迹的跨回合持久化

Revision ID: 0004_message_tool_steps
Revises: 0003_message_feedback
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_message_tool_steps"
down_revision: Union[str, None] = "0003_message_feedback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_tool_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chat_id", sa.String(36), nullable=False),
        # 触发这一回合的用户消息 id，与 trace_spans.message_id 同一个锚点
        sa.Column("message_id", sa.String(36), nullable=True),
        # 0 = 回合开始前的 RAG 预检索，1 起是模型自己决定的轮次
        sa.Column("round_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("call_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_name", sa.String(120), nullable=False),
        sa.Column("tool_call_id", sa.String(80), nullable=True),
        sa.Column("arguments", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="ok"),
        # 原始正文而不是摘要：回灌粒度是会反复调的策略，不该烙进数据
        sa.Column("result_content", sa.Text(), nullable=True),
        sa.Column("result_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("citations", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    # 回灌时按 chat_id + 时间倒序取最近若干步
    op.create_index(
        "ix_message_tool_steps_chat_created",
        "message_tool_steps",
        ["chat_id", "created_at"],
    )
    # 编辑并重新生成时按 message_id 批量清理失效轨迹
    op.create_index(
        "ix_message_tool_steps_message", "message_tool_steps", ["message_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_message_tool_steps_message", table_name="message_tool_steps")
    op.drop_index(
        "ix_message_tool_steps_chat_created", table_name="message_tool_steps"
    )
    op.drop_table("message_tool_steps")
