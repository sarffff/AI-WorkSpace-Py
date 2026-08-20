"""agent_runs / agent_checkpoints：可恢复执行与人工审批

改动之前一个回合的状态活在 SSE 生成器的局部变量里，连接一断就没了。这两张表把
它提成可持久化的东西，于是"一次执行"的生命周期不再等于"一个 HTTP 请求"的生命周期。

``message_tool_steps.run_id`` 是把工具轨迹接到执行记录上的 join 键。可空：历史行
产生于这两张表之前，回填一个假的 run_id 等于声称那时候就有执行记录。

Revision ID: 0010_agent_runs_checkpoints
Revises: 0009_document_parse_meta
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010_agent_runs_checkpoints"
down_revision: Union[str, None] = "0009_document_parse_meta"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chat_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("parent_run_id", sa.String(36), nullable=True),
        sa.Column("agent_role", sa.String(40), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="running"),
        sa.Column("rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delegations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interrupts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("prompt_ref", sa.String(120), nullable=True),
        sa.Column("trace_id", sa.String(32), nullable=True),
        sa.Column("error_type", sa.String(80), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_agent_runs_chat_started", "agent_runs", ["chat_id", "started_at"])
    op.create_index("ix_agent_runs_user_status", "agent_runs", ["user_id", "status"])
    op.create_index("ix_agent_runs_parent", "agent_runs", ["parent_run_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_trace_id", "agent_runs", ["trace_id"])

    op.create_table(
        "agent_checkpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("phase", sa.String(24), nullable=False, server_default="pre_tools"),
        sa.Column("round_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("interrupt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_unique_constraint(
        "uq_agent_checkpoints_run_seq", "agent_checkpoints", ["run_id", "seq"]
    )
    op.create_index(
        "ix_agent_checkpoints_run_seq", "agent_checkpoints", ["run_id", "seq"]
    )

    op.add_column(
        "message_tool_steps",
        sa.Column("run_id", sa.String(36), nullable=True),
    )
    op.create_index("ix_message_tool_steps_run", "message_tool_steps", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_message_tool_steps_run", table_name="message_tool_steps")
    op.drop_column("message_tool_steps", "run_id")

    op.drop_index("ix_agent_checkpoints_run_seq", table_name="agent_checkpoints")
    op.drop_constraint(
        "uq_agent_checkpoints_run_seq", "agent_checkpoints", type_="unique"
    )
    op.drop_table("agent_checkpoints")

    op.drop_index("ix_agent_runs_trace_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_parent", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_chat_started", table_name="agent_runs")
    op.drop_table("agent_runs")
