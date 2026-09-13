"""审核结论台账

审核类任务的交付物不是一段话，是一条**带依据、可复核、能追溯到规程版本**的结论。
在这张表之前它只存在于对话文本里，于是三件事都做不到：

1. **拿不走。** 审完了要交给下游（财务系统、Excel、上级签批），而对话记录不是
   可导出的结构。
2. **复核不了。** 人要重新判断时得把整件事再读一遍，那审核没省下任何东西。
3. **追溯不了。** "这条结论按的是哪一版报销标准"答不出来——SOP 改一次，
   历史结论的依据就指向一份已经不存在的文本。

## 为什么单独一张表，不塞进 message_tool_steps

那张表装的是**工具调用轨迹**（谁在第几轮调了什么、返回了什么），是给排查用的，
按 message 组织、随对话一起被清理。审核结论的生命周期完全不同：它是业务记录，
在对话被删之后仍然必须存在——一张单子的审核结论不该因为有人清理了聊天记录而消失。

所以这里刻意**不设** message 外键约束：``chat_id`` / ``message_id`` 只作为线索
留着（"想看当时怎么审的，去这段对话"），断了不影响这条结论本身的有效性。

## verdict 三档，needs_human 不是失败

``pass`` / ``reject`` / ``needs_human``。第三档是这个产品最有价值的输出：审核这份
工作的全部意义在于"拿不准就往上抬一级"。把它当失败会让人去优化掉它，而那正好
优化掉了价值——所以它在数据模型里就是一等公民，不是某种错误状态。

## inputs 存 JSON，不拆表

一条结论的核对项是 3~6 个、只整体读写、从不单独查询某一项。拆成
``review_verdict_inputs`` 要多一次 join，换来一个用不上的查询能力。
真需要"统计哪一项最常缺失"的时候再拆，那时这张表是现成的数据源。

## sop_version 存快照值，不是外键

引用 ``workspace_skills.version`` 的话，SOP 再改一次这条结论的依据又变了——
而这张表存在的理由正是"当时按的是第几版"。存下来的数字是**事实**，
外键指向的是**现状**。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016_review_verdicts"
down_revision: Union[str, None] = "0015_skill_version_and_required_inputs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "review_verdicts",
        sa.Column("id", sa.String(36), primary_key=True),
        # 作用域。按工作区查台账（"这个月我们审了多少张"），
        # 按用户查"谁审的"——两个都是真实问题，所以两列都要
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        # 线索而非约束（理由见模块文档）：对话被清理之后结论仍然有效
        sa.Column("chat_id", sa.String(36), nullable=True),
        sa.Column("message_id", sa.String(36), nullable=True),
        # 审的是什么。自由文本——它可能是文件名、单号、或者一句描述，
        # 取决于材料从哪来。不做成外键指向文件：文件会被移走、重命名、删除，
        # 而结论要比它长命
        sa.Column("subject", sa.String(500), nullable=False),
        # 按哪份 SOP 审的。版本号是**快照值**不是外键
        sa.Column("sop_name", sa.String(80), nullable=False),
        sa.Column("sop_version", sa.Integer(), nullable=False),
        # pass / reject / needs_human。不用枚举类型：三档还会长
        #（"部分通过"是可预见的下一档），而 MySQL 改枚举要锁表
        sa.Column("verdict", sa.String(20), nullable=False),
        # 逐项核对结果，JSON 数组 [{name, value, found}]
        sa.Column("inputs", sa.Text(), nullable=False),
        # 依据，JSON 数组。人复核时读的就是这一列
        sa.Column("basis", sa.Text(), nullable=False),
        # 一致性检查：采样几次、结论一致吗。
        #
        # runs=1 有两种含义而它们处置不同：没开这个检查，或者开了但只有一次
        # 跑通了。所以 agreed 单独一列——runs=1 且 agreed=true 时它说的是
        # "没检查过"，不是"检查过并且一致"（见 review_consensus.combine）
        sa.Column("runs", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("agreed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # 人复核之后的处置。NULL = 还没人看过。
        # needs_human 的结论要有人接手，而"接手了没有"必须查得出来，
        # 否则转人工等于扔进一个没人看的队列
        sa.Column("resolved_by", sa.String(36), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution", sa.String(20), nullable=True),
        sa.Column("resolution_note", sa.String(1000), nullable=True),
    )
    # 台账按工作区 + 时间倒序翻页，这是最常见的读法
    op.create_index(
        "ix_review_verdicts_ws_created",
        "review_verdicts",
        ["workspace_id", "created_at"],
    )
    # "还有哪些等着人看" —— needs_human 且 resolved_at 为空。
    # 这是待办队列的查询，独立成索引
    op.create_index(
        "ix_review_verdicts_pending",
        "review_verdicts",
        ["workspace_id", "verdict", "resolved_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_verdicts_pending", table_name="review_verdicts")
    op.drop_index("ix_review_verdicts_ws_created", table_name="review_verdicts")
    op.drop_table("review_verdicts")
