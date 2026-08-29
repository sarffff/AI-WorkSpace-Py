"""人工审批的审计线索

审批功能此前只把裁决结果留在快照里（``TurnState.approved_call_ids`` /
``rejected_call_ids`` / ``edited_arguments``），而快照有两个性质让它当不了审计记录：

1. **会被裁剪。** ``AGENT_CHECKPOINT_KEEP`` 只保留最近 N 份，第 N+1 份一来，
   当初那次裁决的证据就没了。
2. **不记录人与时间。** 一个功能的全部意义是"有人授权了这次写入"，而"谁、
   什么时候、看到的参数是哪一版"恰好一个都没存。合规审计问的第一个问题就是这个。

## 为什么是新表而不是 agent_runs 加列

一次执行可以被打断多次（``agent_runs.interrupts`` 就是这个数），所以 run 与裁决
是一对多。加列只能存下最后一次，而"第一次拒绝了、改了参数第二次才批"恰恰是最
需要留痕的形状。

## 为什么存摘要而不是完整参数

``arguments_digest``（SHA-256）+ ``arguments_preview``（截断）而不是整份参数：
写知识库的参数里是整篇文档正文，可以到 ``AGENT_WRITE_MAX_CHARS``（20000 字符）。
把它复制进审计表等于同一份用户内容在库里存两遍，而审计要回答的问题是"当时批准的
到底是不是这一份"——摘要足以证明同一性，预览足以让人认出是哪一份。

digest 算在**执行时真正生效的那份参数**上（用户改过的话就是改后的），所以它能
回答"批准的和执行的是不是同一份"这个问题。

Revision ID: 0012_agent_approvals
Revises: 0011_document_visibility
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012_agent_approvals"
down_revision: Union[str, None] = "0011_document_visibility"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("chat_id", sa.String(36), nullable=True),
        # 发起这次执行的用户。裁决人另有一列——两者通常相同，但不必然：
        # 将来若允许管理员代批，差异正是审计要看的东西。
        sa.Column("user_id", sa.String(36), nullable=False),
        # 被拦下的工具调用
        sa.Column("tool_name", sa.String(80), nullable=False),
        sa.Column("tool_call_id", sa.String(120), nullable=True),
        sa.Column("round_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("call_index", sa.Integer(), nullable=False, server_default="0"),
        # 请求时的参数摘要（模型原本想执行的那一份）
        sa.Column("arguments_digest", sa.String(64), nullable=True),
        sa.Column("arguments_preview", sa.Text(), nullable=True),
        # 裁决：pending / approved / rejected / expired
        #
        # ``expired`` 是 AGENT_APPROVAL_TIMEOUT_HOURS 到点后的终态。它必须和
        # ``rejected`` 分开：拒绝是人做的决定，过期是没人做决定，两者在审计上
        # 完全不是一回事。
        sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
        # 谁裁决的。过期时为 NULL——那正是"没有人"的准确表示
        sa.Column("decided_by", sa.String(36), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        # 用户改过参数的话，这里是**改后**那份的摘要。与 arguments_digest 不同
        # 就说明执行的不是模型原本要执行的东西
        sa.Column("decided_digest", sa.String(64), nullable=True),
        sa.Column("edited_fields", sa.String(255), nullable=True),
        # 用户留的话（拒绝理由 / 修改说明）。已过 mask_markup
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        # 审批界面上那句"为什么要批这个"
        sa.Column("reason", sa.String(255), nullable=True),
    )
    # 按 run 查一次执行的全部裁决（审计详情页）
    op.create_index("ix_agent_approvals_run", "agent_approvals", ["run_id"])
    # 按用户 + 时间查"这个人最近批过什么"（审计列表页）
    op.create_index(
        "ix_agent_approvals_user_requested",
        "agent_approvals",
        ["user_id", "requested_at"],
    )
    # 过期扫描走这个：decision='pending' 且 requested_at 早于阈值
    op.create_index(
        "ix_agent_approvals_decision_requested",
        "agent_approvals",
        ["decision", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_approvals_decision_requested", table_name="agent_approvals")
    op.drop_index("ix_agent_approvals_user_requested", table_name="agent_approvals")
    op.drop_index("ix_agent_approvals_run", table_name="agent_approvals")
    op.drop_table("agent_approvals")
