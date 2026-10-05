"""回复出口：要发给客户的东西先落库，再由幂等的重试发出去。

见 models.TicketOutbox。这一跳补的是文档§4 第 7 步"生成回复 → 更新 CRM → 关闭
工单"里缺的那一环：之前回复只写进了 resolution_note，没有任何东西被发出去，
而所有指标仍然显示这张单办得又快又好。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0024_ticket_outbox"
down_revision: Union[str, None] = "0023_operation_review"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ticket_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        # 线索，非外键：清理工单不该带走一条还没发出去的话
        sa.Column("ticket_id", sa.String(36), nullable=False),
        # reply / csat_invite
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        # pending / sending / sent / failed / suppressed
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.String(200), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    # 认领扫描：按 (status, available_at) 取最老的待发送
    op.create_index(
        "ix_ticket_outbox_status_available", "ticket_outbox", ["status", "available_at"]
    )
    op.create_index("ix_ticket_outbox_ticket", "ticket_outbox", ["ticket_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_outbox_ticket", table_name="ticket_outbox")
    op.drop_index("ix_ticket_outbox_status_available", table_name="ticket_outbox")
    op.drop_table("ticket_outbox")
