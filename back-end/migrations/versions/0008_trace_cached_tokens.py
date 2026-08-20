"""trace_spans.cached_tokens:记录提供商上下文缓存命中的输入 token

智谱等 OpenAI 兼容端点把缓存命中量放在 ``usage.prompt_tokens_details
.cached_tokens``,它是 ``prompt_tokens`` 的子集(不是额外的量)。单独存一列而不是
塞进 attributes JSON:成本要按它拆成"新鲜输入 + 打折的缓存输入"两段来算,而
attributes 是 TEXT,聚合查询里没法 SUM。

存量行为 NULL,含义是"这次调用没有缓存信息",与"命中 0 个"不同——改动之前的
调用根本没读这个字段,回填 0 会让面板上的命中率把历史数据当成"缓存全未命中"。

Revision ID: 0008_trace_cached_tokens
Revises: 0007_workspace_shared_kb
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_trace_cached_tokens"
down_revision: Union[str, None] = "0007_workspace_shared_kb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trace_spans",
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trace_spans", "cached_tokens")
