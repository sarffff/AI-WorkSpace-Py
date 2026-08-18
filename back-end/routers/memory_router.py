"""长期记忆的查看与删除。

抽取是自动的,管理必须是手动的:记忆会随时间积累过时信息("我在 A 组"在
调岗之后就是噪音),用户得能看见系统记了什么、能删掉错的。注入逻辑只读
表,不区分条目来源——被删掉的记忆下一轮就不再注入,即时生效。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services.memory_service import memory_service

router = APIRouter(prefix="/memories", tags=["长期记忆"])


@router.get("")
async def list_memories(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return memory_service.list_memories(db, current_user.id)


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not memory_service.delete_memory(db, memory_id, current_user.id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}
