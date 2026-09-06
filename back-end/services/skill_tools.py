"""``load_skill`` 与 ``read_skill_file``：把作业指导按需取进上下文。

## 为什么是工具而不是自动注入

索引里只有名字和一句描述，正文由模型自己判断要不要取——和 ``delegate`` 同一个形状：
选哪个是**运行时**的决定。做成"按用户问题语义匹配自动注入"的话，匹配错了模型无从
发现（它看不到完整清单），而匹配对了也省不下什么——索引本来就短。

## 为什么另开 read_skill_file 而不让 read_file 读 skill 目录

让文件工具读 skill 目录要动 ``fs_roots.resolve_within_roots``：它得返回"命中的是
哪个根"（现在只返回路径）、要加只读概念、要把 skill 目录并进根列表、还要给模型一个
寻址前缀。那个函数是整个文件能力的**唯一**安全边界，压着二十条逃逸测试。

语义上也会混：``list_directory`` 不带 path 时列的是"用户已授权的文件夹"，
skill 目录混进去之后用户会在自己的工作区列表里看到一堆 SOP 目录——那不是他的文件。

代价是多一个工具名。换来的是沙箱一行不用改，而且 skill 附带文件天然只读
（这个工具没有写的那一半）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from config import settings
from services import skill_library, skill_service
from services.guardrails import guard
from services.skill_library import SkillError
from services.tool_runtime import ToolDefinition


class _LoadedSkills:
    """本回合已经加载过哪些 skill，以及还能加载几个。

    存在的理由有两个，都不是优化：

    1. **同一个 skill 不重复注入正文。** 模型忘了自己加载过是常事（正文在几轮之前
       的上下文里）。重复注入一份几千字的 SOP 会把预算吃掉，而它一个字的新信息都
       没带来。第二次调用返回一句"已经加载过"就够——那句话本身就是提醒。
    2. **上限。** 没有上限时模型会把索引里每一个都加载一遍再开始干活，
       那是最贵的一种"稳妥"。

    状态由调用方从 ``TurnState.loaded_skills`` 恢复，所以审批中断之后接着跑时
    "已经加载过"这件事不会丢。
    """

    __slots__ = ("names", "limit")

    def __init__(self, limit: int, already: list[str] | None = None) -> None:
        self.names: list[str] = list(already or [])
        self.limit = max(0, limit)

    @property
    def exhausted(self) -> bool:
        return len(self.names) >= self.limit


def _build_load_tool(
    db: Session, workspace_id: str, loaded: _LoadedSkills
) -> ToolDefinition:
    async def load_skill(arguments: dict[str, Any]) -> str:
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            return "加载失败：name 必须是非空字符串。"
        wanted = name.strip()

        skills = skill_service.available(db, workspace_id)
        skill = skills.get(wanted)
        if skill is None:
            listed = "、".join(sorted(skills)) or "（本工作区没有登记任何作业指导）"
            # 把可用的名字列出来而不是只说"没找到"：模型下一步该做的是换一个名字
            # 或者放弃，而这两件事都需要知道有哪些
            return f"加载失败：没有名为 {wanted!r} 的作业指导。可用的：{listed}"

        if wanted in loaded.names:
            return (
                f"《{wanted}》已经在本次对话里加载过了，内容就在上文，请直接按它执行。"
            )
        if loaded.exhausted:
            return (
                f"加载失败：本回合最多加载 {loaded.limit} 份作业指导，已经用完。"
                "请基于已加载的内容继续。"
            )

        loaded.names.append(wanted)
        # 工作区 skill 的正文是 admin 写的，是**可信指令**，不过 guard.shield——
        # 它和知识库分块、网页、文件内容不是一类东西：那些是检索来的素材，
        # 这个是组织自己下发的规程。用 shield 包起来反而会让模型把它当素材看待，
        # 而它本来就该被当成指令执行。
        return (
            f"[作业指导《{skill.name}》]\n{skill.instructions}\n"
            + (
                f"\n[本指导附带以下文件，需要时用 read_skill_file 读取："
                f"{'、'.join(skill.attachments)}]"
                if skill.attachments
                else ""
            )
        )

    return ToolDefinition(
        name="load_skill",
        description=(
            "取出某一份作业指导的完整内容。系统上下文里的清单只有名字和用途，"
            "确认某一份和当前任务相关时用它拿正文。开始处理任务之前先看清单。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "作业指导的名字，必须来自上下文里那份清单",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=load_skill,
    )


def _build_read_tool(loaded: _LoadedSkills) -> ToolDefinition:
    async def read_skill_file(arguments: dict[str, Any]) -> str:
        skill_name = arguments.get("skill")
        filename = arguments.get("filename")
        if not isinstance(skill_name, str) or not skill_name.strip():
            return "读取失败：skill 必须是非空字符串。"
        if not isinstance(filename, str) or not filename.strip():
            return "读取失败：filename 必须是非空字符串。"

        # 必须先 load_skill。理由不是流程洁癖：附带文件是那份指导的一部分，
        # 不看指导直接读模板，模型不知道该拿它做什么——而它会自己编一个用法。
        if skill_name.strip() not in loaded.names:
            return (
                f"读取失败：还没有加载《{skill_name.strip()}》。"
                "请先用 load_skill 取它的正文，里面会说明这些附带文件的用途。"
            )

        try:
            path = skill_library.attachment_path(skill_name.strip(), filename.strip())
        except SkillError as exc:
            return f"读取失败：{exc}"

        try:
            with open(path, "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            return f"读取失败：{exc.strerror or exc}"
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = payload.decode("gb18030", errors="replace")

        limit = max(1, settings.SKILL_ATTACHMENT_MAX_CHARS)
        body = text[:limit]
        if len(text) > limit:
            body += f"\n\n[附带文件过长已截断，原文 {len(text)} 字符]"

        # 附带文件**过护栏**，而 skill 正文不过。区别在于：正文是 admin 在界面上
        # 逐字写的，附带文件是一个丢进目录的文件——它可能是从供应商那里拿来的模板、
        # 也可能是某次导出的产物，没有人逐字读过。这是两种不同的可信度。
        shielded, _report = guard.shield(
            f"【{skill_name.strip()} / {filename.strip()}】\n{body}",
            label="作业指导附件",
            kind="read_skill_file",
        )
        return shielded

    return ToolDefinition(
        name="read_skill_file",
        description=(
            "读取某份作业指导附带的文件（模板、参考资料、检查清单）。"
            "只能读该指导声明过的附带文件，且必须先用 load_skill 加载过它。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "作业指导的名字"},
                "filename": {
                    "type": "string",
                    "description": "附带文件名，来自 load_skill 返回里列出的那些",
                },
            },
            "required": ["skill", "filename"],
            "additionalProperties": False,
        },
        handler=read_skill_file,
    )


def build(
    db: Session, workspace_id: str, *, already_loaded: list[str] | None = None
) -> tuple[list[ToolDefinition], _LoadedSkills]:
    """按开关与"这个工作区有没有 skill"组装工具。

    **一份 skill 都没有时不注册。** 注册了的话模型每轮都会看到 ``load_skill``、
    试一次、拿回"没有任何作业指导"——白烧一轮上下文（同 ``workspace_tools.build``
    里那条理由）。

    ``read_skill_file`` 只在**存在带附带文件的内置 skill** 时注册：工作区 skill
    没有附带文件，一个只做纯指令的部署给模型这个工具只会让它去猜文件名。

    返回 ``_LoadedSkills`` 供调用方在回合结束时把名字写回 ``TurnState``。
    """
    loaded = _LoadedSkills(settings.SKILL_MAX_LOADS, already_loaded)
    if not skill_library.enabled():
        return [], loaded
    skills = skill_service.available(db, workspace_id)
    if not skills:
        return [], loaded
    tools = [_build_load_tool(db, workspace_id, loaded)]
    if any(skill.attachments for skill in skills.values()):
        tools.append(_build_read_tool(loaded))
    return tools, loaded


__all__ = ["build"]
