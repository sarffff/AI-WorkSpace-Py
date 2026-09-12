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


class RoleRequest(BaseModel):
    # 不用 Literal["admin","user"]:那样非法值会被 FastAPI 挡成 422,而 422 的
    # detail 是一段 pydantic 结构体,前端那张面板只会显示"请求参数错误"。
    # 让它走到 service 里换一句中文错误("角色只能是 admin 或 user")。
    role: str = Field(..., min_length=1, max_length=20)


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


@router.patch("/members/{member_id}")
async def set_member_role(
    member_id: str,
    body: RoleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """改一个成员的角色(仅管理员)。

    两类失败都回 400,不分 403/404:

    * "仅管理员可以执行"确实是权限问题,但把它和别的分开会让前端要写两条分支,
      而它们的处置一样(把 detail 显示出来)。
    * "该成员不在这个工作区"故意不回 404——404 会确认"这个 id 存在但不在这里",
      也就是把别人的归属漏给调用方。

    这一条与 rename / invite-code 那两个 403 不一致,是刻意的:那两个只有权限
    一种失败,这里有四种(非管理员、人不在、角色非法、最后一个管理员),
    而前端对四种做的是同一件事。
    """
    try:
        result = workspace_service.set_member_role(
            db, current_user, member_id, body.role
        )
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "success": True,
        "member": result,
        "workspace": workspace_service.workspace_info(db, current_user),
    }


@router.delete("/members/{member_id}")
async def remove_member(
    member_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把一个成员移出工作区(仅管理员)。

    响应里带上整份 workspace_info:前端那张面板要同时更新成员列表与
    ``adminCount``(它决定按钮禁不禁),再发一次 GET 是多一趟往返而且会闪。

    被移除的人不会丢数据:共享文档留在原地(组织资产),他自己的私有文档跟他走
    ——不需要迁移,判据是"所有者当前在不在这个空间",见 workspace_service。
    """
    try:
        removed = workspace_service.remove_member(db, current_user, member_id)
    except WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "success": True,
        "removed": removed,
        "workspace": workspace_service.workspace_info(db, current_user),
    }


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
