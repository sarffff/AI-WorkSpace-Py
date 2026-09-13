"""工作区 skill 加版本号与前置材料声明

两列服务同一件事：**让一条审核结论在事后仍然说得清依据**。

## version：规程改了，历史结论不该失去依据

``workspace_skills`` 原来只有 ``updated_at``。于是"这条结论是按哪一版报销标准审
的"答不出来——admin 改一次 SOP，之前所有结论的依据就都指向一份已经不存在的文本。
审核类产品里这是硬伤：三个月后有人问"为什么当时这张单子通过了"，而唯一的答案是
"按当时的标准"，却拿不出当时的标准。

版本号**在 instructions 或 description 变化时才 +1**（由 service 层判定，不是
数据库触发器）：``enabled`` 开关和改错别字不该让版本号跳，否则它很快就大到没人
看，而"版本号变了"这个信号也就失去意义。

不存历史正文，只存一个号。存全量历史要一张 skill_revisions 表，而现在还没有
"回看第 3 版原文"这个需求；先把号钉住，让结论能引用它——需要回看时再补表，
那时这个号正好是外键。

## required_inputs：把"缺前提就别下结论"从措辞变成结构

这一列不是给模型看的提示，是给 ``structured.ReviewVerdict`` 提供必填槽位。结构
里有那个位置，空着就是空着，模型没法悄悄跳过；于是"漏了一项"从判断题（模型该不
该主动问）变成填空题（这一项在不在清单里），后者机械可查。

背景是这个仓库六次实测：请求模型**多做**一件事（记忆 ×3、ask_user ×2、skill ×1）
全部失败，唯一成功的一次是**撤掉一句邀请**。所以这里不再写第七句提示词。

内置 skill 的同一个字段在 ``skills/<name>/SKILL.md`` 的 frontmatter 里，两侧共用
``skill_library.parse_required_inputs`` 解析——各写一遍的话"逗号后的空格算不算"
这种小事迟早分叉，而分叉的表现是同一份 SOP 在两种来源下要求的项数不一样。

存逗号分隔的字符串而不是关联表：它是一个短列表、只整体读写、从不单独查询某一项。
为它开一张表要多一次 join，换来的是一个用不上的查询能力。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015_skill_version_and_required_inputs"
down_revision: Union[str, None] = "0014_workspace_skills"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspace_skills",
        # server_default=1 而不是 nullable：存量行是"第 1 版"，不是"版本未知"。
        # 允许 NULL 会让读取方每处都要写 `or 1`，而漏写一处的表现是台账上
        # 某几条结论的 SOP 版本是空的——那正是这一列要消灭的情形。
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
    )
    op.add_column(
        "workspace_skills",
        # 空串 = 这份 SOP 没有前置材料要求（写作指导类的就没有），
        # 与"有要求但没填"在存储上同形。区分这两者要再加一列，而现在
        # 没有任何处置会因此不同：两种情况下 ReviewVerdict 的 inputs 都是空的。
        sa.Column(
            "required_inputs",
            sa.String(500),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("workspace_skills", "required_inputs")
    op.drop_column("workspace_skills", "version")
