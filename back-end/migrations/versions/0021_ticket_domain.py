"""工单域骨架：工单本体、可回放轨迹、运行时治理开关。

见 models.Ticket / TicketEvent / CsGovernor。项目原本只有"对话 + 知识库"，
Agent 能查不能办。这三张表把它变成工单解决系统：工单是工作单元与指标计数单位，
轨迹让"当时为什么这么决定"在对话被删掉之后仍然查得到（所以 ticket_id 不设外键），
治理开关必须能在事故当中被按下而不能等着改环境变量重启一次。

当日退款已用额度**不落列**：那是能从 cs_operations 聚合出来的事实，存下来就多一个
"计数与实际不一致"的窗口，而它是安全边界。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021_ticket_domain"
down_revision: Union[str, None] = "0020_document_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        # NULL = 还没认出是谁（匿名邮件、只报了订单号）
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("assignee_id", sa.String(36), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("external_ref", sa.String(120), nullable=True),
        # new / understanding / planning / acting / awaiting_approval / escalated /
        # resolved / closed / failed
        sa.Column("status", sa.String(24), nullable=False, server_default="new"),
        # low / mid / high。NULL 读作"还没评估过"，与"评为低风险"是两回事
        sa.Column("risk_level", sa.String(8), nullable=True),
        sa.Column("intent", sa.String(40), nullable=True),
        # 实体抽取结果，json 对象。整体读写
        sa.Column("entities", sa.Text(), nullable=True),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("attachments", sa.Text(), nullable=True),
        sa.Column("summary", sa.String(1000), nullable=True),
        sa.Column("resolution", sa.String(20), nullable=True),
        sa.Column("resolution_note", sa.String(2000), nullable=True),
        sa.Column("escalation_reason", sa.String(40), nullable=True),
        sa.Column("csat_score", sa.Integer(), nullable=True),
        sa.Column("csat_comment", sa.String(1000), nullable=True),
        sa.Column("tool_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_cost", sa.Numeric(10, 4), nullable=True),
        sa.Column("first_response_at", sa.DateTime(), nullable=True),
        sa.Column("sla_due_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        # 渠道重投递去重。唯一约束写在建表里而不是事后 ALTER：SQLite 不支持
        # ALTER 加约束，而这条链在 SQLite 上跑一遍是迁移唯一的离线可验证路径
        # （同 0013/0014 的写法）。external_ref 为 NULL 时放行多行，没有外部 ID
        # 的渠道照常。
        sa.UniqueConstraint(
            "workspace_id", "channel", "external_ref", name="uq_tickets_ws_channel_extref"
        ),
    )
    # 待办队列：按工作区筛状态、按时间倒序
    op.create_index(
        "ix_tickets_ws_status_created",
        "tickets",
        ["workspace_id", "status", "created_at"],
    )
    # 指标聚合：某窗口内的工单
    op.create_index("ix_tickets_ws_created", "tickets", ["workspace_id", "created_at"])
    op.create_index("ix_tickets_customer", "tickets", ["customer_id"])

    op.create_table(
        "ticket_events",
        sa.Column("id", sa.String(36), primary_key=True),
        # 不设外键：轨迹要比对话长命
        sa.Column("ticket_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        # 工单内序号，应用侧取 max+1（同 audit_log.seq）
        sa.Column("seq", sa.Integer(), nullable=False, server_default="1"),
        # intake / understand / risk / retrieve / plan / act / confirm / escalate
        sa.Column("node", sa.String(24), nullable=False),
        # thinking / tool_call / tool_result / approval / decision / state_change /
        # error / reply
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=True),
        sa.Column("tool_call_id", sa.String(64), nullable=True),
        sa.Column("args_digest", sa.String(64), nullable=True),
        sa.Column("args_preview", sa.Text(), nullable=True),
        sa.Column("result_excerpt", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("round_index", sa.Integer(), nullable=True),
        sa.Column("cost", sa.Numeric(10, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    # 回放：按工单取整条轨迹，顺序就是 seq
    op.create_index("ix_ticket_events_ticket_seq", "ticket_events", ["ticket_id", "seq"])
    op.create_index(
        "ix_ticket_events_ws_created", "ticket_events", ["workspace_id", "created_at"]
    )

    op.create_table(
        "cs_governors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("pause_reason", sa.String(200), nullable=True),
        sa.Column("pause_updated_by", sa.String(36), nullable=True),
        # 限额列可空：NULL = 沿用全局默认，与 0（一律不许）分得开
        sa.Column("daily_refund_limit", sa.Numeric(12, 2), nullable=True),
        sa.Column("max_cost_per_ticket", sa.Numeric(10, 4), nullable=True),
        sa.Column("per_ticket_tool_calls", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", name="uq_cs_governors_workspace"),
    )


def downgrade() -> None:
    op.drop_table("cs_governors")
    op.drop_index("ix_ticket_events_ws_created", table_name="ticket_events")
    op.drop_index("ix_ticket_events_ticket_seq", table_name="ticket_events")
    op.drop_table("ticket_events")
    op.drop_index("ix_tickets_customer", table_name="tickets")
    op.drop_index("ix_tickets_ws_created", table_name="tickets")
    op.drop_index("ix_tickets_ws_status_created", table_name="tickets")
    op.drop_table("tickets")
