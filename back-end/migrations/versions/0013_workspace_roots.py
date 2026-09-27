"""本机文件夹授权（文件系统工具的沙箱根）

文件系统工具（``list_directory`` / ``read_file`` / ``write_file`` / ``edit_file``
/ ``delete_file`` / ``search_files``）只能在这张表里登记过的目录下工作。表是空的
时候这些工具**根本不注册**——没有授权目录，它们除了报错什么都做不了。

## 为什么按 user 而不是按 workspace

授权是**本机行为**：用户在自己的机器上点了一次系统对话框，选了一个目录。
而 workspace 是多人共享的概念（邀请码加入、admin/member 两种角色）。按 workspace
存的话，一个成员授权的目录会让同工作区的另一个人读到——而那个人的机器上根本
没有这个目录，或者更糟，有一个同路径但内容完全不同的目录。

``workspace_id`` 仍然存下来，但它是**记录**而不是判据：用来回答"这次授权是在哪个
工作区的上下文里给的"。判据只有 ``user_id``。

## 为什么不放 settings_service 的 preferences

那一层是 Redis 或进程内存（``services/settings_service.py``）。Redis 没开时它退回
内存字典，进程一重启授权就没了——而用户不会理解"我明明授权过"。文件夹授权必须
比一次进程生命周期活得久。

## 为什么 path 是原样存的绝对路径

不做规范化改写。``resolve_within_roots`` 每次校验时都会对**两边**重新
``os.path.realpath``，那才是符号链接会变的地方：入库时解析一次并不能保证之后
那个链接指向的还是同一处。存原样也让"已授权文件夹"列表显示的和用户当初在对话框
里选的是同一个字符串。

唯一约束是 ``(user_id, path)``：同一个人重复授权同一个目录是幂等的，而不是攒出
两行让撤销只撤掉一半。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013_workspace_roots"
down_revision: Union[str, None] = "0012_agent_approvals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspace_roots",
        sa.Column("id", sa.String(36), primary_key=True),
        # 判据。文件系统工具只认这一列
        sa.Column("user_id", sa.String(36), nullable=False),
        # 记录用：这次授权是在哪个工作区的上下文里给的。不参与权限判断
        sa.Column("workspace_id", sa.String(36), nullable=True),
        # 用户在系统对话框里选的那个绝对路径，原样存。
        # 512 而不是 255：Windows 长路径 + 中文目录名很容易超过 255 字节，
        # 而截断的后果是沙箱根变成一个**不同的目录**——前缀校验会通过，
        # 但通过的是错的那个前缀。
        sa.Column("path", sa.String(512), nullable=False),
        # 界面上显示的名字。默认取目录名，允许用户改成"工作资料"这类说法
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "path", name="uq_workspace_roots_user_path"),
    )
    op.create_index(
        "ix_workspace_roots_user_id", "workspace_roots", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_roots_user_id", table_name="workspace_roots")
    op.drop_table("workspace_roots")
