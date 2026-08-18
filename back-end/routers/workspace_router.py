"""工作区接口:成员查看共享知识库的归属,管理员管理邀请码。

知识库文档的上传/删除在 /knowledge 下(已按角色门控),这里只负责
"我所在的工作区是什么、有谁、怎么拉人进来"。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services import workspace_service
from services.workspace_service import WorkspaceError

router = APIRouter(prefix="/workspace", tags=["工作区"])


@router.get("")
async def get_workspace(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户的工作区信息。旧用户首次调用会自动初始化个人空间。"""
    return workspace_service.workspace_info(db, current_user)


@router.post("/invite-code")
async def regenerate_invite_code(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """重置邀请码(仅管理员)。旧码立即作废。"""
    try:
        code = workspace_service.regenerate_invite_code(db, current_user)
    except WorkspaceError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"inviteCode": code}
