"""应用内通知收件箱

审批挂起 / ask_user / prose_question / 超时废弃这些"有事等你"的事件，眼下只在那条
实时 SSE 连接上存在一瞬，刷新或断线即消失。这张表把它们落成持久的 per-user 收件箱
（见 models.Notification 与 services/notification_service.py）。

不设外键：run_id / chat_id 只作深链线索，断了不影响通知本身。read_at 为 NULL 即未读。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0019_notifications"
down_revision: Union[str, None] = "0018_audit_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        # 收件人：要处理这件事的那个人
        sa.Column("user_id", sa.String(36), nullable=False),
        # approval_required / input_required / run_abandoned
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        # 深链线索，非外键
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("chat_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # NULL = 未读
        sa.Column("read_at", sa.DateTime(), nullable=True),
    )
    # 收件箱按时间倒序翻页
    op.create_index(
        "ix_notifications_user_created", "notifications", ["user_id", "created_at"]
    )
    # 未读计数 / 未读筛选
    op.create_index(
        "ix_notifications_user_read", "notifications", ["user_id", "read_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_read", table_name="notifications")
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
