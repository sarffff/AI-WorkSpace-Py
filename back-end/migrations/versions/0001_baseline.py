"""baseline: 引入 Alembic 之前由 create_all 建立的表

这一条是「基线」，不是普通迁移：它把 Alembic 之前已经存在的 schema
（users / chats / messages / documents / document_chunks / prompts）
纳入版本管理。已有数据库应该 ``alembic stamp`` 到这个版本而不是执行它
（应用启动时会自动完成，见 database.init_db）。

基线刻意直接用 Base.metadata 建表——它要表达的就是「当时 models.py 是什么样」。
从下一条迁移开始必须写显式的 op.create_table / op.add_column：
迁移是历史快照，不能跟着 models.py 一起漂移。

Revision ID: 0001_baseline
Revises:
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 基线只覆盖引入 Alembic 之前就存在的表，trace_spans 由 0002 显式创建
_BASELINE_TABLES = (
    "users",
    "chats",
    "messages",
    "documents",
    "document_chunks",
    "prompts",
)


def _baseline_metadata():
    import models  # noqa: F401  确保全部表定义已注册
    from database import Base

    return Base.metadata


def upgrade() -> None:
    metadata = _baseline_metadata()
    tables = [metadata.tables[name] for name in _BASELINE_TABLES if name in metadata.tables]
    metadata.create_all(bind=op.get_bind(), tables=tables, checkfirst=True)


def downgrade() -> None:
    metadata = _baseline_metadata()
    tables = [metadata.tables[name] for name in _BASELINE_TABLES if name in metadata.tables]
    metadata.drop_all(bind=op.get_bind(), tables=tables, checkfirst=True)
