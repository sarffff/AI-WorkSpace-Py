"""documents.parse_backend / parse_warnings:解析溯源与入库自检的结果

这条链路上最常见的失败**全都不抛异常**:扫描版 PDF 抽出空文本、GBK 文档被
``errors="replace"`` 解成一串 U+FFFD、PDF 里没识别出任何标题层级。改动之前它们
一律落成 ``status="indexed"``,界面上和一篇正常文档毫无区别,只是永远检索不到。

于是 ``failed`` 这个状态本身也不够用:它只说明"没建成",说不清是编码坏了、是
扫描件、还是 embedding 接口挂了——而这三者的处理办法完全不同。这两列就是
"为什么这篇文档不对"的唯一记录。

``parse_warnings`` 存 JSON 数组而不是建一张关联表:它只被读来展示与排查,从不
参与聚合或连接。空列表存 NULL,于是 ``parse_warnings IS NOT NULL`` 就等于
"这篇文档有话要说"。

存量行两列都是 NULL,含义是"入库时还没有这套自检",不是"自检通过"。

Revision ID: 0009_document_parse_meta
Revises: 0008_trace_cached_tokens
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009_document_parse_meta"
down_revision: Union[str, None] = "0008_trace_cached_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("parse_backend", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("parse_warnings", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "parse_warnings")
    op.drop_column("documents", "parse_backend")
