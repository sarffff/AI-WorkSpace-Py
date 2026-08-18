"""工作区(组织)服务:共享知识库的作用域与角色。

设计原则:
- **懒初始化**:旧用户与 OAuth 新用户的 workspace_id 为空,第一次访问
  工作区相关功能时 resolve_for_user 自动补建个人空间(admin)。注册、
  登录、OAuth 回调都不必为此各写一遍。
- **两级角色**:admin 管理文档与邀请码,member 只读知识库。不引入更细的
  权限矩阵——两级之外的需求出现之前,复杂度都是负债。
- 个人空间不是特殊形态:注册时不带邀请码的人同样是"自己空间的 admin",
  代码里只有一条路径。
"""
from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from models import User, Workspace

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

# 去掉易混淆字符(0/O、1/I),邀请码会被人口抄、微信群转发
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_invite_code() -> str:
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(8))


class WorkspaceError(Exception):
    """业务错误,由路由层转成 4xx。message 面向最终用户。"""


def resolve_for_user(db: Session, user: User) -> Workspace:
    """确保用户已有所属工作区,返回它。

    没有(旧用户/OAuth 新用户)就自动建一个个人空间并设为 admin。
    幂等:并发调用最多多建一个空间,不会报错——以用户行上最终指向为准。
    """
    if user.workspace_id:
        workspace = db.query(Workspace).filter(Workspace.id == user.workspace_id).first()
        if workspace is not None:
            return workspace
    display = (user.name or user.username or user.email or "个人")[:100]
    workspace = Workspace(
        name=f"{display}的空间",
        invite_code=_unique_invite_code(db),
    )
    db.add(workspace)
    db.flush()
    user.workspace_id = workspace.id
    user.role = ROLE_ADMIN
    db.commit()
    db.refresh(workspace)
    return workspace


def _unique_invite_code(db: Session) -> str:
    """碰撞概率 32^8 ≈ 1.1e12,重试几次足够;真撞了说明随机源坏了。"""
    for _ in range(5):
        code = _new_invite_code()
        exists = db.query(Workspace).filter(Workspace.invite_code == code).first()
        if exists is None:
            return code
    raise WorkspaceError("邀请码生成失败，请重试")


def join_by_invite_code(db: Session, user: User, code: str) -> Workspace:
    """凭邀请码加入工作区,成为 member。找不到码时抛业务错误。"""
    normalized = code.strip().upper()
    workspace = (
        db.query(Workspace).filter(Workspace.invite_code == normalized).first()
    )
    if workspace is None:
        raise WorkspaceError("邀请码无效")
    if user.workspace_id == workspace.id:
        raise WorkspaceError("你已在该工作区中")
    user.workspace_id = workspace.id
    user.role = ROLE_MEMBER
    db.commit()
    db.refresh(workspace)
    return workspace


def require_admin(user: User) -> None:
    """管理操作(上传/删除文档、重置邀请码)的统一闸口。"""
    if user.role != ROLE_ADMIN:
        raise WorkspaceError("仅工作区管理员可以执行此操作")


def workspace_info(db: Session, user: User) -> dict:
    """前端展示用。邀请码只给 admin:member 看不到就不会转发给不该进来的人。"""
    workspace = resolve_for_user(db, user)
    members = (
        db.query(User)
        .filter(User.workspace_id == workspace.id)
        .order_by(User.created_at.asc())
        .all()
    )
    return {
        "id": workspace.id,
        "name": workspace.name,
        "role": user.role,
        "memberCount": len(members),
        "members": [
            {"id": m.id, "name": m.name or m.username or m.email, "role": m.role}
            for m in members
        ],
        "inviteCode": workspace.invite_code if user.role == ROLE_ADMIN else None,
    }


def regenerate_invite_code(db: Session, user: User) -> str:
    """重置邀请码。旧码立即作废——泄露后的止损动作。"""
    require_admin(user)
    workspace = resolve_for_user(db, user)
    workspace.invite_code = _unique_invite_code(db)
    db.commit()
    return workspace.invite_code
