"""工作区:workspaces 表 + users.role/workspace_id + documents.workspace_id

知识库的作用域从"用户"改为"工作区":同工作区成员共享文档,admin 管理成员读。
回填策略:存量用户每人自动建一个以其命名的空间并设为 admin,其上传的文档
归入该空间——行为与升级前完全一致(一人一库),之后靠邀请码把人拉进同一空间。

Revision ID: 0007_workspace_shared_kb
Revises: 0006_content_hash_user_memory
"""
import secrets
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_workspace_shared_kb"
down_revision: Union[str, None] = "0006_content_hash_user_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 邀请码字符表:去掉易混淆的 0/O 与 1/I
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_invite_code() -> str:
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(8))


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("invite_code", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_workspaces_invite_code", "workspaces", ["invite_code"], unique=True)

    op.add_column("users", sa.Column("workspace_id", sa.String(36), nullable=True))
    op.create_index("ix_users_workspace_id", "users", ["workspace_id"])
    op.add_column("users", sa.Column("role", sa.String(20), nullable=False, server_default="admin"))

    op.add_column("documents", sa.Column("workspace_id", sa.String(36), nullable=True))
    op.create_index("ix_documents_workspace_id", "documents", ["workspace_id"])

    # 回填:每个存量用户一个空间(admin),文档归入上传者的空间。
    # 迁移里用 Python 生成 uuid/邀请码,而不是依赖数据库函数——方言差异
    # (MySQL 的 UUID() vs SQLite 无此函数)会让迁移在评估环境里炸掉。
    conn = op.get_bind()
    users = conn.execute(sa.text("SELECT id, name, username FROM users")).fetchall()
    for user_id, name, username in users:
        workspace_id = str(uuid.uuid4())
        display = (name or username or user_id)[:100]
        conn.execute(
            sa.text(
                "INSERT INTO workspaces (id, name, invite_code) "
                "VALUES (:id, :name, :code)"
            ),
            {"id": workspace_id, "name": f"{display}的空间", "code": _new_invite_code()},
        )
        conn.execute(
            sa.text("UPDATE users SET workspace_id = :ws WHERE id = :uid"),
            {"ws": workspace_id, "uid": user_id},
        )
        conn.execute(
            sa.text(
                "UPDATE documents SET workspace_id = :ws "
                "WHERE user_id = :uid AND workspace_id IS NULL"
            ),
            {"ws": workspace_id, "uid": user_id},
        )


def downgrade() -> None:
    op.drop_index("ix_documents_workspace_id", table_name="documents")
    op.drop_column("documents", "workspace_id")
    op.drop_index("ix_users_workspace_id", table_name="users")
    op.drop_column("users", "role")
    op.drop_column("users", "workspace_id")
    op.drop_index("ix_workspaces_invite_code", table_name="workspaces")
    op.drop_table("workspaces")
