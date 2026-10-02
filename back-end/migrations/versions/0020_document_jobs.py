"""入库任务队列：持久化的文档索引任务（替代进程内 BackgroundTasks）。

见 models.DocumentJob 与 services/document_queue.py——为什么需要它、形态为何照搬
agent_runs 的 lease/reaper。不设外键到 documents：删文档时孤儿任务无害，下一次认领
时 index_document 对"文档已消失"优雅返回、任务自然 complete。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0020_document_jobs"
down_revision: Union[str, None] = "0019_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), nullable=False),
        # queued / running / succeeded / failed
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        # 最早可认领时刻（退避的载体）
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("error", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    # 认领扫描：按 (status, available_at) 取最老的可认领任务
    op.create_index(
        "ix_document_jobs_status_available",
        "document_jobs",
        ["status", "available_at"],
    )
    # 入队去重 / 按文档查任务
    op.create_index("ix_document_jobs_document", "document_jobs", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_document_jobs_document", table_name="document_jobs")
    op.drop_index("ix_document_jobs_status_available", table_name="document_jobs")
    op.drop_table("document_jobs")
