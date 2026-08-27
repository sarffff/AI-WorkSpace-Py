"""文档可见性:工作区共享 / 个人私有

加一列 ``documents.visibility``（``workspace`` | ``private``）。配套的角色语义是
admin 管共享文档、user 只能管自己的私有文档（见 ``services/workspace_service``）。

**存量数据一律置 workspace**。这一列出现之前所有文档都是工作区共享语义，迁移必须
保持原样——默认成 private 就等于升级一次把团队知识库全部变成某个人的私有资料，
而且那份资料的归属取决于当初谁上传，几乎不可能人工还原。

顺带说明为什么没有 0011_drop_workspace_invite_code：那一版曾经写过（下线邀请码、
人人自建个人空间），但**从未 apply**（库停在 0010，``invite_code`` 列仍在），
而现在的方案重新需要邀请码。所以直接删掉了那个文件，而不是留一对互相抵消的迁移——
留着只会让以后读迁移史的人以为邀请码被移除过又加回来。

Revision ID: 0011_document_visibility
Revises: 0010_agent_runs_checkpoints
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011_document_visibility"
down_revision: Union[str, None] = "0010_agent_runs_checkpoints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default 而不是只给 ORM 默认值：存量行要立刻有值，而且这一列参与
    # 权限过滤，NULL 会让"是共享还是私有"变成一个需要 COALESCE 的三态问题。
    op.add_column(
        "documents",
        sa.Column(
            "visibility",
            sa.String(16),
            nullable=False,
            server_default="workspace",
        ),
    )
    # 检索的每一次查询都带 visibility 条件，建索引。
    # 单列而不是 (workspace_id, visibility) 复合：workspace_id 已经有自己的索引，
    # 而私有文档的过滤还要带 user_id，真正该做复合索引的组合等有了查询计划再定。
    op.create_index("ix_documents_visibility", "documents", ["visibility"])


def downgrade() -> None:
    op.drop_index("ix_documents_visibility", table_name="documents")
    op.drop_column("documents", "visibility")
