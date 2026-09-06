"""作业指导（skill）的查看与管理。

内置 skill 在仓库里（``back-end/skills/``），只读——改它要改代码、走 review。
工作区 skill 在数据库里，**只有 admin 能增删改**：一条 SOP 影响的是全工作区所有人
的执行方式，那和"上传一份自己的资料"不是一个权限级别。

普通成员能看（他们需要知道 agent 会按什么规程办事），不能改。

## 为什么改完要清语义缓存

改 SOP 的目的就是让之后的回答不一样。缓存不清的话同一个问题会继续返回按旧规程
生成的答案，而用户刚刚才改过它——那是"改了没生效"的典型形状，且无从发现。
和知识库变更走同一个入口（``semantic_cache.invalidate_user``）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services import skill_service, workspace_service
from services.semantic_cache import semantic_cache
from services.skill_library import SkillError

router = APIRouter(prefix="/skills", tags=["作业指导"])


class SkillUpsertRequest(BaseModel):
    # 模型要用这个名字调 load_skill，所以限长且不许空格（校验在 service 层，
    # 那里能给出可读的错误消息）
    name: str = Field(min_length=1, max_length=80)
    # 模型选用这份指导的**唯一依据**
    description: str = Field(min_length=1, max_length=255)
    instructions: str = Field(min_length=1)
    enabled: bool = True


def _require_admin(user: User) -> None:
    """一条 SOP 影响全工作区所有人的执行方式，不是"上传自己的资料"那个级别。"""
    if not workspace_service.is_admin(user):
        raise HTTPException(
            status_code=403, detail="只有工作区管理员可以修改作业指导"
        )


@router.get("")
async def list_skills(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """两层分开列：它们能做的操作不同（内置只读）。

    普通成员也能看——他们需要知道 agent 会按什么规程办事。
    """
    payload = skill_service.list_for_admin(db, current_user.workspace_id or "")
    payload["canEdit"] = workspace_service.is_admin(current_user)
    return payload


@router.put("")
async def upsert_skill(
    body: SkillUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新增或按 name 更新。

    用 PUT + upsert 而不是 POST/PATCH 分开：admin 最常做的操作是"改一条 SOP 的
    正文"，而拆成两个端点会让前端先判断它是不是新的——那个判断在服务端做更准
    （唯一约束就在那里）。
    """
    _require_admin(current_user)
    if not current_user.workspace_id:
        raise HTTPException(status_code=400, detail="当前用户还没有工作区")
    try:
        row = skill_service.upsert(
            db,
            current_user.workspace_id,
            name=body.name,
            description=body.description,
            instructions=body.instructions,
            enabled=body.enabled,
            created_by=current_user.id,
        )
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 改 SOP 的目的就是让之后的回答不一样。不清缓存的话同一个问题会继续按旧规程答。
    semantic_cache.invalidate_user(current_user.id)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
    }


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(current_user)
    if not skill_service.delete(
        db, current_user.workspace_id or "", skill_id
    ):
        raise HTTPException(status_code=404, detail="该作业指导不存在")
    semantic_cache.invalidate_user(current_user.id)
    return {"ok": True}
