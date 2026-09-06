"""Skill：可被按需加载的作业指导。

一个 skill 回答"这件事在本组织该怎么做"——报销怎么审、季度报告怎么写、代码评审
看哪几项。它是**指令**，不是工具：不带独立的执行上下文，加载之后由当前这个代理
自己照着做。

## 和子代理角色（``agent_roles``）的区别

角色回答"谁来做"：它有自己的循环、自己的工具子集、自己的轮次预算，产出是一份
交给主代理的报告。skill 回答"怎么做"：它只是一段追加进上下文的指令。

所以两者不冲突也不重叠——角色**可以**加载 skill。而角色是硬编码在
``agent_roles.ROLES`` 里的（企业用户加不了），skill 两层都能加。

## 两层：内置 + 工作区

- **内置**（``back-end/skills/<name>/SKILL.md``）：跟代码版本化、能 review、
  可以带附带文件（模板、参考资料）。
- **工作区**（``workspace_skills`` 表）：admin 在界面上写，不改代码不重启。
  没有附带文件——那是内置 skill 独有的。

同名时**工作区盖内置**：各家自己的 SOP 优先于通用模板。

## 为什么判据是 workspace_id 而不是 user_id

和 ``workspace_roots`` 恰好相反。文件夹授权是**本机行为**（这台机器上的这个目录），
而 SOP 是**组织资产**（全公司同一套报销流程）。按 user 存的话每个员工都要自己录
一遍，而且他们会录出互相矛盾的版本。

## 索引很短，正文按需加载

只把"名字 + 一句描述"注入上下文，模型自己判断该用哪个、调 ``load_skill`` 拿正文。
几十个 SOP 全塞进去的话，每一轮都要付这笔固定成本，而其中至多一个和当前问题有关。

这也是为什么 ``description`` 是必填的：它是模型做选择的**唯一**依据，
写不好等于这个 skill 不存在。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from config import settings
from services import file_types

logger = logging.getLogger("skill_library")

SKILL_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills")

# SKILL.md 里必须有的 frontmatter 字段
_REQUIRED_KEYS = ("name", "description")

# 附带文件允许的扩展名。只收文本——skill 的附带文件是给模型读的模板与参考资料，
# 二进制格式它读不了，收下只会让"为什么读不出来"变成一个要查的问题。
#
# 从 ``file_types`` 派生，**不在这里列**：扩展名清单在这个仓库里曾经有六处互相
# 矛盾的副本，``test_file_types_single_source`` 就是为了不再长出下一处而存在的
# （我这里确实就地列了一份，那条测试立刻抓住了——和写 fs_tools 时一模一样）。
_ATTACHMENT_EXTENSIONS = file_types.SKILL_ATTACHMENT


class SkillError(RuntimeError):
    """skill 缺字段、目录结构不对、名字冲突——一律在加载时立刻抛出。

    和 ``PromptError`` 同一个取舍：宁可在启动时起不来，也不要等第一个用户提问
    才发现某个 SOP 的 description 是空的（那时它已经静默地从索引里消失了）。
    """


@dataclass(frozen=True, slots=True)
class Skill:
    """一个 skill。内置与工作区两种来源共用这个形状。"""

    name: str
    description: str
    instructions: str
    # "builtin" | "workspace"。界面上要区分（内置只读），
    # 埋点里也要能回答"用的是通用模板还是本公司自己写的"
    source: str
    # 附带文件的**文件名**（不是路径）。只有内置 skill 有。
    # 存文件名而不是绝对路径：路径要在读取时重新拼并校验，
    # 存下来的路径过一段时间就可能指向别处（同 fs_roots 里那条理由）。
    attachments: tuple[str, ...] = ()

    def index_line(self) -> str:
        """注入索引时的一行。"""
        return f"- {self.name}：{self.description}"


def _parse_frontmatter(raw: str, where: str) -> tuple[dict[str, str], str]:
    """解析 ``---`` 包起来的 frontmatter，返回 ``(字段, 正文)``。

    手写而不是引 PyYAML：这里只需要 ``key: value`` 一层，而 skill 正文里出现
    YAML 特殊字符（冒号、缩进、列表符号）是常态——真拿 YAML 解析器去读整个文件
    反而更容易炸在正文上。
    """
    if not raw.startswith("---"):
        raise SkillError(f"{where}：缺少 frontmatter（文件必须以 --- 开头）")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise SkillError(f"{where}：frontmatter 没有闭合（需要第二个 ---）")
    meta: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise SkillError(f"{where}：frontmatter 里这一行不是 key: value —— {line!r}")
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    return meta, parts[2].strip()


def _load_builtin() -> dict[str, Skill]:
    """扫 ``skills/`` 目录。目录不存在时返回空——skill 是可选功能。"""
    if not os.path.isdir(SKILL_DIR):
        return {}
    skills: dict[str, Skill] = {}
    for entry in sorted(os.listdir(SKILL_DIR)):
        directory = os.path.join(SKILL_DIR, entry)
        if not os.path.isdir(directory):
            continue
        manifest = os.path.join(directory, "SKILL.md")
        if not os.path.isfile(manifest):
            raise SkillError(f"skills/{entry}：缺少 SKILL.md")
        with open(manifest, "r", encoding="utf-8") as handle:
            meta, body = _parse_frontmatter(handle.read(), f"skills/{entry}/SKILL.md")
        for key in _REQUIRED_KEYS:
            if not meta.get(key):
                raise SkillError(f"skills/{entry}/SKILL.md：frontmatter 缺 {key}")
        if not body:
            raise SkillError(f"skills/{entry}/SKILL.md：正文是空的")
        # 目录名与 name 必须一致：不一致时 load_skill 该按哪个查是个没有好答案的
        # 问题，而附带文件是按目录找的。
        if meta["name"] != entry:
            raise SkillError(
                f"skills/{entry}/SKILL.md：name 是 {meta['name']!r}，"
                "必须和目录名一致"
            )
        attachments = tuple(
            sorted(
                filename
                for filename in os.listdir(directory)
                if filename != "SKILL.md"
                and os.path.isfile(os.path.join(directory, filename))
                and filename.rsplit(".", 1)[-1].lower() in _ATTACHMENT_EXTENSIONS
            )
        )
        skills[entry] = Skill(
            name=entry,
            description=meta["description"],
            instructions=body,
            source="builtin",
            attachments=attachments,
        )
    return skills


_builtin: dict[str, Skill] | None = None


def builtin() -> dict[str, Skill]:
    global _builtin
    if _builtin is None:
        _builtin = _load_builtin()
    return _builtin


def reload() -> dict[str, Skill]:
    """丢缓存重新读盘。给测试和本地调试用——改 skill 不必重启进程。"""
    global _builtin
    _builtin = None
    return builtin()


def validate() -> int:
    """启动时调用。宁可在这里起不来，也不要等第一个用户提问才炸。"""
    count = len(reload())
    logger.info("skill library: %s builtin skill(s)", count)
    return count


def enabled() -> bool:
    return settings.SKILL_ENABLED


def attachment_path(skill_name: str, filename: str) -> str:
    """内置 skill 附带文件的真实路径。**校验都在这里**。

    两道：文件名必须在该 skill 登记过的附带文件里（白名单，而不是拼路径再判
    有没有越界），拼出来的路径还要落在该 skill 目录之内。

    第一道就足以挡住 ``../../.env``——那个字符串不在任何 skill 的附带文件列表里。
    第二道是冗余的，留着是因为这里的输入同样来自模型，而白名单一旦以后改成
    "按扩展名放行"就只剩第二道了。
    """
    skill = builtin().get(skill_name)
    if skill is None:
        raise SkillError(f"没有名为 {skill_name!r} 的内置 skill。")
    if filename not in skill.attachments:
        listed = "、".join(skill.attachments) or "（无）"
        raise SkillError(
            f"{skill_name} 没有名为 {filename!r} 的附带文件。可用的：{listed}"
        )
    root = os.path.realpath(os.path.join(SKILL_DIR, skill_name))
    target = os.path.realpath(os.path.join(root, filename))
    if target != root and not target.startswith(root + os.sep):
        raise SkillError("附带文件路径超出 skill 目录。")
    if not os.path.isfile(target):
        raise SkillError(f"附带文件 {filename} 不存在。")
    return target


__all__ = [
    "SKILL_DIR",
    "Skill",
    "SkillError",
    "attachment_path",
    "builtin",
    "enabled",
    "reload",
    "validate",
]
