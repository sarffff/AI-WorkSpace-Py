from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services import prompt_library
from services.prompt_service import PromptService

router = APIRouter(prefix="/prompts", tags=["提示词"])
prompt_service = PromptService()


class PromptCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    category: str = "General"
    content: str = Field(..., min_length=1)
    is_public: bool = False


class PromptUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    content: Optional[str] = None
    is_public: Optional[bool] = None


@router.get("")
async def list_prompts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取提示词列表（当前用户的 + 公开的）"""
    return await prompt_service.list_prompts(db, user_id=current_user.id)


@router.get("/library")
async def get_prompt_library(
    current_user: User = Depends(get_current_user),
):
    """系统提示词注册表（只读）。

    只读是刻意的：这些版本是仓库里的文件，要经过 code review 和 git 历史。
    如果开一个"在线编辑系统提示词"的接口，评估跑出的分数就不再对应任何
    可回溯的版本——"上次那组数是哪版提示词跑的"会变成没人能回答的问题。
    想试新写法，就在 prompts/<key>/ 下加一个版本文件。
    """
    return {"entries": prompt_library.catalog()}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_prompt(
    body: PromptCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建提示词模板"""
    p = await prompt_service.create_prompt(
        db,
        title=body.title,
        description=body.description,
        category=body.category,
        content=body.content,
        user_id=current_user.id,
        is_public=body.is_public,
    )
    return PromptService._to_dict(p)


@router.patch("/{prompt_id}")
async def update_prompt(
    prompt_id: str,
    body: PromptUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新提示词模板（仅作者可改）"""
    updated = await prompt_service.update_prompt(
        db,
        prompt_id,
        user_id=current_user.id,
        title=body.title,
        description=body.description,
        category=body.category,
        content=body.content,
        is_public=body.is_public,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="提示词不存在或无权限")
    return PromptService._to_dict(updated)


@router.delete("/{prompt_id}")
async def delete_prompt(
    prompt_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除提示词模板（仅作者可删）"""
    ok = await prompt_service.delete_prompt(db, prompt_id, current_user.id)
    if not ok:
        raise HTTPException(status_code=404, detail="提示词不存在或无权限")
    return {"success": True}
