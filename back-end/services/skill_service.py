"""skill 的两层合并、索引渲染、以及 CRUD。

``skill_library`` 只管内置那一层（读盘、校验、附带文件）。这个模块把它和数据库
那一层合起来，产出"这个工作区此刻能用哪些 skill"。

## 合并规则：工作区盖内置

同名时工作区那份胜出。不做"两份正文拼在一起"——拼起来之后哪一句生效取决于模型，
而那不可预测。各家自己的 SOP 该盖过通用模板，这一条要能一句话说清。

停用（``enabled=False``）的工作区 skill **既不出现在索引里，也盖不掉内置**：
"关掉我们自己那版"最自然的期待是退回通用模板，而不是连通用模板一起消失。

## 索引为什么是独立的 system 消息

照 ``memory_service.build_system_block`` 的先例：提示词是带版本管理的"代码"，
skill 索引是逐工作区增长的"数据"。混进主提示词会让同一版提示词在不同工作区之间
表现不可比，也破坏语义缓存按 ``prompt_ref`` 分桶的前提。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from config import settings
from models import WorkspaceSkill
from services import skill_library
from services.guardrails import mask_markup
from services.skill_library import Skill, SkillError

logger = logging.getLogger("skill_service")

_INDEX_HEADER = (
    "[可用的作业指导（skill）]\n"
    "下面是本工作区登记的作业指导。它们只列出了名字和用途——需要某一份的完整内容时，"
    "调用 load_skill 取。\n"
    "开始处理一个任务之前先看这份清单：如果有对应的作业指导，先加载它再动手，"
    "不要凭自己的通用做法去做已经有规定的事情。清单里没有对应项时按常规方式处理即可。"
)


def _workspace_skills(db: Session, workspace_id: str) -> dict[str, WorkspaceSkill]:
    if not workspace_id:
        return {}
    rows = (
        db.query(WorkspaceSkill)
        .filter(WorkspaceSkill.workspace_id == workspace_id)
        .order_by(WorkspaceSkill.name.asc())
        .all()
    )
    return {row.name: row for row in rows}


def available(db: Session, workspace_id: str) -> dict[str, Skill]:
    """这个工作区此刻能用的 skill。工作区那层盖内置。

    停用的工作区 skill 直接跳过——它既不进索引也不盖内置，于是"关掉我们自己那版"
    退回通用模板，而不是把这个能力整个关掉。
    """
    merged: dict[str, Skill] = dict(skill_library.builtin())
    for name, row in _workspace_skills(db, workspace_id).items():
        if not row.enabled:
            continue
        merged[name] = Skill(
            name=name,
            description=row.description,
            instructions=row.instructions,
            source="workspace",
            # 工作区 skill 没有附带文件（理由见迁移 0014 的文档）
            attachments=(),
            # 和内置 skill 的 frontmatter 共用一套解析：两处各写一遍的话
            # "逗号后的空格算不算"迟早分叉，而分叉的表现是同一份 SOP 在两种
            # 来源下要求的项数不一样——不报错，只是审核松了一档。
            required_inputs=skill_library.parse_required_inputs(row.required_inputs),
        )
    return merged


def version_of(db: Session, workspace_id: str, skill: Skill) -> int:
    """这份 SOP 当前的版本号。审核结论要记下它。

    **内置 skill 返回 0**，含义是"版本由代码仓库决定，不由这张表决定"。
    它跟着 git 走，没有行级版本号可言；给个 1 会让台账上"第 1 版"同时指两件事
    （内置的、以及工作区那份从没改过的），而这两者的追溯方式完全不同——
    前者去查那次部署的 commit，后者查 workspace_skills。

    0 不是"未知"或"出错"：迁移 0016 给工作区行的 server_default 是 1，
    所以 0 只会出现在内置这一种情形上，读得出来。

    按 ``skill.source`` 判而不是"查库看有没有行"：同名时工作区那份盖内置
    （见 ``available``），而被盖掉的那一刻 source 就是 workspace 了——
    查库会在"有行但停用了、实际用的是内置"这个情形上给出错的版本号。
    """
    if skill.source != "workspace":
        return 0
    row = _workspace_skills(db, workspace_id).get(skill.name)
    # 拿不到行却声称是 workspace 来源，说明调用方手上的 Skill 比库里的旧
    # （比如刚被别人删了）。1 是那张表的起始值，比抛异常合适：
    # 台账少一次记录比整次审核失败更糟。
    return (row.version if row else 1) or 1


def build_index_block(db: Session, workspace_id: str) -> str:
    """注入用的索引。没有任何 skill 时返回空串（调用方据此不发这条消息）。

    索引里只放名字和描述，**不放正文**：一个企业几十个 SOP，全塞进去的话每一轮都
    要付这笔固定成本，而其中至多一个和当前问题有关。

    描述过 ``mask_markup``：工作区 skill 的描述是 admin 写的，但 admin 也可能从别处
    复制粘贴。这一句会出现在系统上下文里，可信度很高，所以标记语法要中和掉。
    """
    skills = available(db, workspace_id)
    if not skills:
        return ""
    limit = max(1, settings.SKILL_INDEX_MAX_ITEMS)
    names = sorted(skills)
    shown = names[:limit]
    lines = [_INDEX_HEADER]
    lines += [
        f"- {skills[name].name}：{mask_markup(skills[name].description)}"
        for name in shown
    ]
    if len(names) > limit:
        # 如实说明被截断，否则模型会以为清单是完整的
        lines.append(
            f"（本工作区共 {len(names)} 份作业指导，此处只列出前 {limit} 份）"
        )
    return "\n".join(lines)


# ========== 相关性：这一轮该不该给 skill 让路 ==========

# skill 描述的向量缓存。key 是 (skill 名, 描述的哈希)——描述变了就自然换 key，
# 不需要显式失效。skill 的数量是几十条量级，且描述很短，常驻内存没有压力。
#
# 不按 workspace_id 分桶:同一份内置 skill 在每个工作区的描述是同一句话,
# 按工作区分会把同一个向量算 N 遍。
_DESCRIPTION_VECTORS: dict[tuple[str, int], list[float]] = {}


async def most_relevant(
    db: Session,
    workspace_id: str,
    question: str,
    *,
    embedding: Any,
) -> tuple[str, float] | None:
    """和这个问题最相关的那份 skill，以及相似度。没有 skill 时返回 None。

    ## 这个函数只回答"要不要让路"，不回答"该用哪份"

    它的返回值只被用来决定**本轮跳不跳过预检索**。挑哪一份、要不要真的加载，
    仍然是模型看着索引自己调 ``load_skill`` 决定的——那是刻意保留的分工:
    框架来判断"该用哪份 SOP"的话，判错比不加载更糟（照着错误的流程办事，
    而输出看起来一样合理）。

    ## 为什么用向量而不是关键词

    中文分词在这件事上不可靠:「审报销单」和「报销单审核」共享的字很多，
    而「发票能不能不开」和「报销额度标准」几乎不共享字，但后者才是同一件事。
    描述本来就是一句话，向量化很便宜，而 embedding 走的是免费模型（见
    model_prices.json 里 bge-m3 那条）。

    描述向量按内容缓存;问题向量每轮算一次——这是唯一的新增成本。
    """
    skills = available(db, workspace_id)
    if not skills:
        return None

    ordered = sorted(skills)
    missing = [
        name
        for name in ordered
        if (name, hash(skills[name].description)) not in _DESCRIPTION_VECTORS
    ]
    if missing:
        # 描述连着名字一起向量化。只用描述的话，名字里带的信息（"expense-review"）
        # 就丢了，而 admin 起的名字往往比描述更贴题。
        texts = [f"{skills[name].name}：{skills[name].description}" for name in missing]
        vectors = await embedding.embed_texts(texts)
        for name, vector in zip(missing, vectors):
            _DESCRIPTION_VECTORS[(name, hash(skills[name].description))] = vector

    question_vector = await embedding.embed_query(question)
    if not question_vector:
        return None

    best: tuple[str, float] | None = None
    for name in ordered:
        vector = _DESCRIPTION_VECTORS.get((name, hash(skills[name].description)))
        if not vector:
            continue
        score = embedding.cosine_similarity(question_vector, vector)
        if best is None or score > best[1]:
            best = (name, score)
    return best


def reset_vector_cache() -> None:
    """清掉描述向量缓存。给测试用——描述哈希已经保证了正确性，
    但用例之间共享缓存会让"第几次调用 embed_texts"这类断言互相干扰。
    """
    _DESCRIPTION_VECTORS.clear()


# ========== CRUD ==========


def list_for_admin(db: Session, workspace_id: str) -> dict[str, Any]:
    """管理界面用：两层分开列，因为它们能做的操作不同（内置只读）。"""
    workspace = _workspace_skills(db, workspace_id)
    return {
        "builtin": [
            {
                "name": skill.name,
                "description": skill.description,
                "attachments": list(skill.attachments),
                # 内置的声明来自 frontmatter，和工作区那侧同一个键名，
                # 好让界面上两层用同一个渲染
                "requiredInputs": ", ".join(skill.required_inputs),
                # 内置 skill 被同名工作区 skill 盖掉时要显示出来，
                # 否则 admin 会以为自己写的那份没生效
                "overridden": name in workspace and workspace[name].enabled,
            }
            for name, skill in sorted(skill_library.builtin().items())
        ],
        "workspace": [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "instructions": row.instructions,
                "enabled": row.enabled,
                "requiredInputs": row.required_inputs or "",
                # 审核结论引用的就是这个号。列出来才能对上"这条结论按的哪一版"
                "version": row.version or 1,
                "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in sorted(workspace.values(), key=lambda item: item.name)
        ],
        "enabled": skill_library.enabled(),
    }


def upsert(
    db: Session,
    workspace_id: str,
    *,
    name: str,
    description: str,
    instructions: str,
    enabled: bool = True,
    created_by: str | None = None,
    required_inputs: str = "",
) -> WorkspaceSkill:
    """新增或更新一条工作区 skill。

    按 ``(workspace_id, name)`` upsert 而不是"重名报错"：admin 在界面上改一条 SOP
    的正文是最常见的操作，让它变成"先删再建"会丢掉 created_by 与创建时间。

    ## version 什么时候 +1

    只在 ``instructions`` 或 ``description`` 真的变了的时候。理由分两头：

    - **不能不涨。** 审核结论会引用 ``sop_version``（见 ``ReviewVerdict``）。
      不涨的话 admin 改一次 SOP，之前所有结论的依据就都指向一份已经不存在的文本
      ——三个月后有人问"当时为什么通过"，答不出来。
    - **不能乱涨。** ``enabled`` 开关、以及"保存了但一个字没改"都不该让它跳。
      每次保存都 +1 的话这个号很快大到没人看，"版本变了"这个信号也就没用了。

    ``required_inputs`` 变化**也算**：它直接决定 ``ReviewVerdict`` 有几个必填槽位，
    也就是直接决定审核的严格程度。改了它而版本号不动，等于悄悄放宽了标准。
    """
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise SkillError("name 不能为空。")
    # 模型要用这个名字调 load_skill，所以不允许空格和奇怪字符——
    # 带空格的名字模型抄进参数时很容易多一个或少一个
    if not all(char.isalnum() or char in "-_" for char in cleaned_name):
        raise SkillError("name 只能包含字母、数字、连字符和下划线。")
    if not (description or "").strip():
        raise SkillError("description 不能为空——它是模型选用这份指导的唯一依据。")
    if not (instructions or "").strip():
        raise SkillError("instructions 不能为空。")
    limit = max(1, settings.SKILL_MAX_CHARS)
    if len(instructions) > limit:
        raise SkillError(f"instructions 超过 {limit} 字符，请精简或拆成两份。")

    row = (
        db.query(WorkspaceSkill)
        .filter(
            WorkspaceSkill.workspace_id == workspace_id,
            WorkspaceSkill.name == cleaned_name,
        )
        .first()
    )
    now = datetime.now()
    cleaned_description = description.strip()[:255]
    # 存回去的是**归一化之后**的字符串，不是用户原样输入的那行：比较时才对得上，
    # 否则把「金额，凭证」改成「金额, 凭证」也会算成一次改动、白涨一个版本号。
    cleaned_inputs = ", ".join(
        skill_library.parse_required_inputs(required_inputs)
    )[:500]
    if row is None:
        row = WorkspaceSkill(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            name=cleaned_name,
            description=cleaned_description,
            instructions=instructions,
            enabled=enabled,
            required_inputs=cleaned_inputs,
            # 第 1 版。不是 0——结论里引用「第 0 版」读起来像"还没有版本"
            version=1,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        # 先判再写。写完再比就永远相等了，这是这类"变了才 +1"最容易踩的一脚。
        changed = (
            row.instructions != instructions
            or row.description != cleaned_description
            or (row.required_inputs or "") != cleaned_inputs
        )
        row.description = cleaned_description
        row.instructions = instructions
        row.enabled = enabled
        row.required_inputs = cleaned_inputs
        if changed:
            # `or 1` 兜住存量行:0015 给了 server_default,但已经在内存里的对象
            # 可能是迁移之前读出来的
            row.version = (row.version or 1) + 1
        row.updated_at = now
    db.commit()
    db.refresh(row)
    return row


def delete(db: Session, workspace_id: str, skill_id: str) -> bool:
    """删一条工作区 skill。过滤条件必须带 workspace_id——少了它，
    拿一个 id 就能删别的工作区的 SOP。
    """
    row = (
        db.query(WorkspaceSkill)
        .filter(
            WorkspaceSkill.id == skill_id,
            WorkspaceSkill.workspace_id == workspace_id,
        )
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


__all__ = [
    "available",
    "build_index_block",
    "delete",
    "list_for_admin",
    "most_relevant",
    "reset_vector_cache",
    "upsert",
    "version_of",
]
