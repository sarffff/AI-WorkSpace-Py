"""审核结论留下依据原文

``submit_review`` 现在要求模型把它据以判断的材料原文一起交上来。这一列存那份原文。

## 它同时解决两件事

1. **人复核时要看的就是这个。** 台账上原来只有结论和"依据"（引用了哪一条），
   而复核的人要判断"这个引用对不对"时，还得回去翻对话记录——那时审核并没有
   省下多少事。
2. **独立复审有输入了。** 一致性检查（``review_consensus.generate``）要拿同一份
   材料重新判一次，而工具处理器看不到对话上下文：模型读到的东西只存在于
   ``messages`` 里，那是循环的状态，不是工具的参数。

第 2 条本来有另一条路——把 ``messages`` 传进工具边界。没选它：那会让工具实现
耦合上循环状态，而 ``approval.build_preview`` 的文档串里正好反对过同一件事
（算 diff 要读磁盘、要 Session，所以那个计算留在调用方）。

代价是模型要多抄一遍材料，费 token，而且**它可能抄得不全**。抄不全的后果是
复审看到的材料比第一次少，于是更容易给出 needs_human——方向是安全的
（多转人工，不是少转），但会让一致性检查的假阳性变多。这一点没有好办法在
结构上排除，只能靠 REVIEW_EVIDENCE_MAX_CHARS 给足空间。

## 为什么是 Text 而不是 String(n)

材料可能是一整张报销单的 OCR 文本、几十行的合同条款。给一个具体上限就一定会有
被截断的那天，而截断发生在**入库时**、静默、且截掉的正好是尾部——而尾部常常
是签字与日期。入参那侧有 REVIEW_EVIDENCE_MAX_CHARS 挡着，那里超限会明确报错。

## nullable=True

存量的 review_verdicts 行没有这一列的数据，而它们仍然是有效的结论。
NULL 读作"这条结论是加这一列之前记的"，与"材料是空的"（空串）分得开——
后者是个 bug，前者是历史。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0017_review_evidence"
down_revision: Union[str, None] = "0016_review_verdicts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "review_verdicts",
        sa.Column("evidence", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("review_verdicts", "evidence")
