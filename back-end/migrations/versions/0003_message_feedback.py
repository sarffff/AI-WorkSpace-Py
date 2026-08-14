"""新增 message_feedback：消息级用户反馈

Revision ID: 0003_message_feedback
Revises: 0002_trace_spans
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_message_feedback"
down_revision: Union[str, None] = "0002_trace_spans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), nullable=False),
        sa.Column("chat_id", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        # up / down 两档
        sa.Column("rating", sa.String(8), nullable=False),
        sa.Column("reason", sa.String(32), nullable=True),
        # 用户主动提交的标注文本，会随反馈导出进评估集
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("expected_answer", sa.Text(), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("exported_at", sa.DateTime(), nullable=True),
    )
    # 一条消息只留一份反馈：改主意是更新，不是再追加一个负样本
    op.create_unique_constraint(
        "uq_message_feedback_message", "message_feedback", ["message_id"]
    )
    op.create_index("ix_message_feedback_user_id", "message_feedback", ["user_id"])
    op.create_index(
        "ix_message_feedback_user_created", "message_feedback", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_message_feedback_rating", "message_feedback", ["rating", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_message_feedback_rating", table_name="message_feedback")
    op.drop_index("ix_message_feedback_user_created", table_name="message_feedback")
    op.drop_index("ix_message_feedback_user_id", table_name="message_feedback")
    op.drop_constraint(
        "uq_message_feedback_message", "message_feedback", type_="unique"
    )
    op.drop_table("message_feedback")
