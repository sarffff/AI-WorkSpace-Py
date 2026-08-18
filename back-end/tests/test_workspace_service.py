"""工作区:懒初始化、邀请码加入、角色闸口与信息聚合。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Document, User, Workspace
from services import workspace_service
from services.workspace_service import WorkspaceError

import models  # noqa: F401  确保所有表已注册


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()


def _user(db, email="u1@example.com", **kwargs) -> User:
    user = User(email=email, username=email.split("@")[0], role="admin", **kwargs)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_resolve_creates_personal_workspace_for_legacy_user(db):
    user = _user(db)  # 未挂工作区(旧用户/OAuth 新用户的形态)
    assert user.workspace_id is None

    workspace = workspace_service.resolve_for_user(db, user)

    assert user.workspace_id == workspace.id
    assert user.role == workspace_service.ROLE_ADMIN
    assert workspace.invite_code and len(workspace.invite_code) == 8
    # 幂等:再次 resolve 返回同一个空间,不会重复建
    assert workspace_service.resolve_for_user(db, user).id == workspace.id


def test_join_by_invite_code_makes_member(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)

    member = _user(db, email="u2@example.com")
    joined = workspace_service.join_by_invite_code(db, member, workspace.invite_code.lower())

    assert joined.id == workspace.id
    assert member.role == workspace_service.ROLE_MEMBER

    with pytest.raises(WorkspaceError):
        workspace_service.join_by_invite_code(db, member, "ZZZZZZZZ")  # 无效码
    with pytest.raises(WorkspaceError):
        workspace_service.join_by_invite_code(db, member, workspace.invite_code)  # 已在其中


def test_require_admin_blocks_member(db):
    admin = _user(db)
    workspace_service.resolve_for_user(db, admin)
    workspace_service.require_admin(admin)  # admin 通过

    member = _user(db, email="u2@example.com")
    member.role = workspace_service.ROLE_MEMBER
    with pytest.raises(WorkspaceError):
        workspace_service.require_admin(member)


def test_workspace_info_hides_invite_code_from_member(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)

    info = workspace_service.workspace_info(db, admin)
    assert info["inviteCode"] == workspace.invite_code
    assert info["memberCount"] == 1

    member = _user(db, email="u2@example.com")
    workspace_service.join_by_invite_code(db, member, workspace.invite_code)
    member_info = workspace_service.workspace_info(db, member)
    # member 看得到成员列表,看不到邀请码
    assert member_info["inviteCode"] is None
    assert member_info["memberCount"] == 2
    assert {m["role"] for m in member_info["members"]} == {"admin", "member"}


def test_regenerate_invite_code_invalidates_old(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    old_code = workspace.invite_code

    new_code = workspace_service.regenerate_invite_code(db, admin)

    assert new_code != old_code
    assert db.query(Workspace).filter(Workspace.invite_code == old_code).first() is None


def test_documents_are_scoped_by_workspace(db):
    """不同工作区的文档互不可见——共享不是全局。"""
    import asyncio

    from services.knowledge_service import KnowledgeService

    u1, u2 = _user(db), _user(db, email="u2@example.com")
    ws1 = workspace_service.resolve_for_user(db, u1)
    ws2 = workspace_service.resolve_for_user(db, u2)

    db.add(
        Document(
            name="ws1-doc.md", size=10, content="# a", workspace_id=ws1.id,
            user_id=u1.id, status="indexed", chunks=1,
        )
    )
    db.commit()

    service = KnowledgeService.__new__(KnowledgeService)  # 不建 HTTP 客户端
    docs_ws1 = asyncio.run(service.get_documents(db, ws1.id))
    docs_ws2 = asyncio.run(service.get_documents(db, ws2.id))

    assert [d["name"] for d in docs_ws1] == ["ws1-doc.md"]
    assert docs_ws2 == []
