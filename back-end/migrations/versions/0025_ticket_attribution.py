"""把观测与审计的归属从"一次对话"改成"一张工单"。

工作台时代每条埋点/审计都挂着 ``chat_id`` / ``run_id``，那两个东西现在没有对应
实体了：编排运行就是工单的运行，线程 id 等于 ticket id。留着一个永远为 NULL 的
列比删掉它更糟——下一个读代码的人会以为存在某种"运行"身份，然后去 join 一张
已经没人写的表。

所以这里只做**改名与收敛**，不做删表：chats / messages / agent_runs 这些旧表里
是真实历史数据，重构代码不等于授权删除用户历史。要不要 drop 由运维按自己的保留
窗口决定（本仓库不提供那条迁移）。

- ``trace_spans.chat_id`` → ``ticket_id``（索引一起改名）；``message_id`` 删掉——
  工单域没有"触发这次回答的消息"这个概念，工单本身就是那个锚点。
- ``notifications.run_id`` → ``ticket_id``；``chat_id`` 删掉。
- ``audit_log.run_id`` → ``ticket_id``；``chat_id`` 删掉。

三张表都不设外键（原设计如此：留痕不该阻止业务数据被删），所以改名不涉及约束。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0025_ticket_attribution"
down_revision: Union[str, None] = "0024_ticket_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 改名之后把旧值清掉：那些值是 chat_id / run_id，不是任何一张工单。留着的话
    # 工单台按 ticket_id 反查埋点会捞到不相关的行，而点进深链会跳到一个不存在的
    # 工单——一个假的线索比没有线索更贵。
    op.drop_index("ix_trace_spans_chat_started", table_name="trace_spans")
    # batch_alter_table 而不是裸 alter_column：SQLite 不实现 ALTER RENAME/DROP COLUMN，
    # 只有 batch 的 copy-and-move 能过；MySQL 那边 recreate="auto" 会退回直接 ALTER，
    # 所以这不是一次全表拷贝的惩罚，只是让同一份迁移在两种库上都成立。
    with op.batch_alter_table("trace_spans") as batch:
        batch.alter_column(
            "chat_id",
            new_column_name="ticket_id",
            existing_type=sa.String(36),
            nullable=True,
        )
        batch.drop_column("message_id")
    op.execute("UPDATE trace_spans SET ticket_id = NULL")
    op.create_index(
        "ix_trace_spans_ticket_started", "trace_spans", ["ticket_id", "started_at"]
    )

    with op.batch_alter_table("notifications") as batch:
        batch.alter_column(
            "run_id",
            new_column_name="ticket_id",
            existing_type=sa.String(36),
            nullable=True,
        )
        batch.drop_column("chat_id")
    op.execute("UPDATE notifications SET ticket_id = NULL")

    with op.batch_alter_table("audit_log") as batch:
        batch.alter_column(
            "run_id",
            new_column_name="ticket_id",
            existing_type=sa.String(36),
            nullable=True,
        )
        batch.drop_column("chat_id")
    op.execute("UPDATE audit_log SET ticket_id = NULL")


def downgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.drop_column("ticket_id")
        batch.add_column(sa.Column("chat_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("run_id", sa.String(36), nullable=True))

    with op.batch_alter_table("notifications") as batch:
        batch.drop_column("ticket_id")
        batch.add_column(sa.Column("chat_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("run_id", sa.String(36), nullable=True))

    op.drop_index("ix_trace_spans_ticket_started", table_name="trace_spans")
    with op.batch_alter_table("trace_spans") as batch:
        batch.add_column(sa.Column("message_id", sa.String(36), nullable=True))
        batch.alter_column(
            "ticket_id",
            new_column_name="chat_id",
            existing_type=sa.String(36),
            nullable=True,
        )
    op.create_index(
        "ix_trace_spans_chat_started", "trace_spans", ["chat_id", "started_at"]
    )
