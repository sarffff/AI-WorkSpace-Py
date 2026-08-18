"""documents 加 content_hash(去重键) + 新建 user_memories 表(跨会话长期记忆)

两个改动回答的是同一个问题:"用户反复使用系统时,哪些状态应该留下来"。
知识库侧:同一内容传两遍会占两套 chunk 并挤掉 top_k 里的其他文档,
哈希去重让重复上传变成幂等操作。记忆侧:滚动摘要只活在单个会话内,
user_memories 让"用户是谁、偏好什么"能跨会话沉淀。

Revision ID: 0006_content_hash_user_memory
Revises: 0005_tool_step_agent_role
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_content_hash_user_memory"
down_revision: Union[str, None] = "0005_tool_step_agent_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("content_hash", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_documents_content_hash", "documents", ["content_hash"]
    )
    op.create_table(
        "user_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="fact"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_user_memories_user_created", "user_memories", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_memories_user_created", table_name="user_memories")
    op.drop_table("user_memories")
    op.drop_index("ix_documents_content_hash", table_name="documents")
    op.drop_column("documents", "content_hash")
