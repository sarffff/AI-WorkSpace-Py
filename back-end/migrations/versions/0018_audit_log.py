"""防篡改审计链

跨动作的哈希链审计（见 services/audit_log.py 与 models.AuditLog）。已有的
``agent_approvals`` 记审批的详细状态、``trace_spans`` 是可观测性埋点；这张表回答
的是合规要问的另一个问题——"这串改变状态的动作有没有被事后改过"。

## 为什么 seq 不是自增主键

``seq`` 是**每个 actor 链内**的序号（record 时取该 actor 当前最大值 +1），主键仍是
uuid ``id``（与全表一致）。不用数据库自增：BIGINT 自增在 SQLite / MySQL 之间的行为
不一致，而"这是第几条"是链内相对量，与其他用户的写入无关。

## 索引

只建复合索引 ``(actor_id, seq)``：链的遍历（verify 按 seq 升序）、自作用域读取
（按 actor_id 过滤）都走它，actor_id 前缀查询也被它覆盖，无需再单列 actor 索引。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018_audit_log"
down_revision: Union[str, None] = "0017_review_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        # 该 actor 链内的序号，从 1 起（不是数据库自增，理由见模块文档）
        sa.Column("seq", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("actor_id", sa.String(36), nullable=False),
        # tool.write / approval.decision / auth.login / auth.register …
        sa.Column("action", sa.String(40), nullable=False),
        # 动作作用于什么：工具名、文档 id、或一句短描述
        sa.Column("target", sa.String(255), nullable=True),
        # 参数摘要与预览，复用 approval_audit 的算法（sorted-json sha256 + 截断）
        sa.Column("args_digest", sa.String(64), nullable=True),
        sa.Column("args_preview", sa.Text(), nullable=True),
        # 线索，非外键：审计不该阻止业务数据被删
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("chat_id", sa.String(36), nullable=True),
        # 链：prev_hash = 上一条 entry_hash（首条为 genesis 全零串）
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("entry_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_audit_log_actor_seq", "audit_log", ["actor_id", "seq"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_actor_seq", table_name="audit_log")
    op.drop_table("audit_log")
