"""message_tool_steps 增加 agent_role：区分主代理与子代理执行的步骤

委派模式下一条轨迹里会混着两种步骤：主代理自己调的工具，和它委派出去的子代理
调的工具。不区分的话「这次回答查了 8 次知识库」既可能是主代理反复检索，也可能是
一次委派里 researcher 查了 6 次——这两种情况的改进方向完全相反，前者要收紧提示词，
后者说明委派本身是有效的。

NULL 表示主代理,而不是给一个 'supervisor' 的默认值:已有的历史行确实是单代理时期
产生的,回填一个角色名等于声称那时候就有主/子之分。

Revision ID: 0005_tool_step_agent_role
Revises: 0004_message_tool_steps
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_tool_step_agent_role"
down_revision: Union[str, None] = "0004_message_tool_steps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "message_tool_steps",
        sa.Column("agent_role", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("message_tool_steps", "agent_role")
