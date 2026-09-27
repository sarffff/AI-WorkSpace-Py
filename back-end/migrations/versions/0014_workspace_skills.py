"""工作区自定义 skill

skill 是"这件事在本组织该怎么做"的作业指导（报销怎么审、季度报告怎么写）。内置
skill 放在仓库里跟代码版本化（``back-end/skills/``），这张表装的是**各家自己的
SOP**——admin 在界面上写，不改代码、不重启进程。

## 为什么判据是 workspace_id 而不是 user_id

和 ``workspace_roots``（迁移 0013）恰好相反，两者的对比正好说明判据是怎么定的：

- 文件夹授权是**本机行为**：这台机器上的这个目录，换个人就不成立。
- SOP 是**组织资产**：全公司同一套报销流程。按 user 存的话每个员工都要自己录
  一遍，而且他们会录出互相矛盾的版本——那时"到底按谁的流程审"没有答案。

## 同名时盖掉内置

``(workspace_id, name)`` 唯一。查找时工作区优先：各家自己的 SOP 该盖过通用模板。
不做"合并两份正文"——两段指令拼在一起时哪一句生效取决于模型，而那是不可预测的。

## 为什么不存附带文件

内置 skill 可以在目录里带模板与参考资料，这张表不行。存附带文件意味着要么往库里
塞 blob、要么在磁盘上给每个工作区开目录，而后者又要一套自己的路径校验。工作区
skill 的定位是"一段能立刻改的指令"，需要模板的场景走内置或知识库。

## enabled 而不是直接删

改坏一条 SOP 之后想先关掉看看，比"删了再重新录一遍"便宜得多。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014_workspace_skills"
down_revision: Union[str, None] = "0013_workspace_roots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspace_skills",
        sa.Column("id", sa.String(36), primary_key=True),
        # 判据。skill 是组织资产,不跟人走
        sa.Column("workspace_id", sa.String(36), nullable=False),
        # 模型用它调 load_skill,所以要短、要像标识符。同工作区内唯一
        sa.Column("name", sa.String(80), nullable=False),
        # **模型选 skill 的唯一依据**。写不好等于这个 skill 不存在——
        # 它只出现在索引里,而索引是模型看到的全部
        sa.Column("description", sa.String(255), nullable=False),
        # 正文。load_skill 返回的就是这一段
        sa.Column("instructions", sa.Text(), nullable=False),
        # 关掉而不是删掉:改坏一条 SOP 之后想先停用看看
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # 谁写的。SOP 出问题时第一个要问的就是这个
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "name", name="uq_workspace_skills_ws_name"
        ),
    )
    op.create_index(
        "ix_workspace_skills_workspace_id", "workspace_skills", ["workspace_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_skills_workspace_id", table_name="workspace_skills"
    )
    op.drop_table("workspace_skills")
