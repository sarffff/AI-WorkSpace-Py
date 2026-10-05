"""写操作的人工复盘台账：让"错误操作率"这个指标有真数据源。

见 models.CsOperation 里那一段说明。一句话版本：错误操作率的定义是**执行了但不该
执行**，而系统内部只能证明"这一笔通过了当时所有自动检查"，两者之间的那道判断
只能由人来下——所以那是一道列，不是一个查询。

只有 executed/replayed 的行值得被标注；被拦下和失败的行已经有自己的状态，
把它们混进分母只会让率值随拦截量抖动。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0023_operation_review"
down_revision: Union[str, None] = "0022_cs_business_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cs_operations", sa.Column("reviewed_by", sa.String(36), nullable=True))
    # ok / wrong。NULL = 还没人看过
    op.add_column("cs_operations", sa.Column("verdict", sa.String(8), nullable=True))
    op.add_column("cs_operations", sa.Column("corrected_at", sa.DateTime(), nullable=True))
    # 判成 wrong 之后人实际怎么补救的
    op.add_column("cs_operations", sa.Column("corrected_action", sa.String(40), nullable=True))
    op.add_column("cs_operations", sa.Column("review_note", sa.String(500), nullable=True))
    # 错误操作率的聚合形状：某窗口内已标注的执行操作里有多少 wrong
    op.create_index(
        "ix_cs_operations_ws_verdict",
        "cs_operations",
        ["workspace_id", "verdict", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cs_operations_ws_verdict", table_name="cs_operations")
    op.drop_column("cs_operations", "review_note")
    op.drop_column("cs_operations", "corrected_action")
    op.drop_column("cs_operations", "corrected_at")
    op.drop_column("cs_operations", "verdict")
    op.drop_column("cs_operations", "reviewed_by")
