"""工作区接口:归属、成员、邀请码。

知识库文档的上传/删除在 /knowledge 下(按角色与可见性门控),这里只负责
"我在哪个工作区、有谁、怎么加入"。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services import workspace_service
from services.workspace_service import WorkspaceError

router = APIRouter(prefix="/workspace", tags=["工作区"])


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class JoinRequest(BaseModel):
    # 长度上界给 16 而不是 8:邀请码长度是实现细节,而这里只需要挡住超长输入。
    # 真正的校验在 join_by_invite_code 里(它做 strip + upper 再查库)。
    invite_code: str = Field(..., min_length=1, max_length=16)


@router.get("")
async def get_workspace(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户的工作区信息。旧用户首次调用会自动初始化个人空间。"""
    return workspace_service.workspace_info(db, current_user)


@router.post("/join")
async def join_workspace(
    body: JoinRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """凭邀请码加入工作区，成为 user 角色。

    **加入是换空间，不是多一个空间**（``User.workspace_id`` 是单值外键）。所以
    响应里带上原空间还剩多少篇文档：那些文档不会被删，但加入后不再出现在任何检索里。
    静默切换是不可接受的——用户会以为自己的资料丢了。

    真正的多空间归属需要一张成员关联表，那是另一件事。
    """
    previous = workspace_service.resolve_for_user(db, current_user)
    previous_documents = await _document_count(db, previous.id, current_user.id)
    try:
        workspace = workspace_service.join_by_invite_code(
            db, current_user, body.invite_code
        )
    except WorkspaceError as e:
        # 400 而不是 403：码无效、已在其中都是输入问题，不是权限问题
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "success": True,
        "workspace": workspace_service.workspace_info(db, current_user),
        # 前端据此提示"原空间的 N 篇文档将不再出现在检索里"
        "leftBehindDocuments": previous_documents,
    }


@router.patch("")
async def rename_workspace(
    body: RenameRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """改工作区名(仅管理员)。"""
    try:
        workspace_service.rename(db, current_user, body.name)
    except WorkspaceError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return workspace_service.workspace_info(db, current_user)


@router.post("/invite-code")
async def regenerate_invite_code(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """重置邀请码(仅管理员)。旧码立即作废——泄露后的止损动作。"""
    try:
        code = workspace_service.regenerate_invite_code(db, current_user)
    except WorkspaceError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"inviteCode": code}


async def _document_count(db: Session, workspace_id: str, viewer_id: str) -> int:
    """离开前那个空间里,这个人能看到多少篇文档。

    算的是"他看得见的"而不是"空间里全部的":对一个即将离开的人来说,
    有意义的数字是他自己会失去访问的那些。
    """
    from services.knowledge_service import KnowledgeService

    documents = await KnowledgeService().get_documents(
        db, workspace_id, viewer_id=viewer_id
    )
    return len(documents)
