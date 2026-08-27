"""工作区:懒初始化、角色闸口与信息聚合。"""
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
    # 幂等:再次 resolve 返回同一个空间,不会重复建
    assert workspace_service.resolve_for_user(db, user).id == workspace.id


def test_require_admin_blocks_member(db):
    admin = _user(db)
    workspace_service.resolve_for_user(db, admin)
    workspace_service.require_admin(admin)  # admin 通过

    member = _user(db, email="u2@example.com")
    member.role = workspace_service.ROLE_USER
    with pytest.raises(WorkspaceError):
        workspace_service.require_admin(member)


def test_workspace_info_lists_members_with_roles(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)

    info = workspace_service.workspace_info(db, admin)
    assert info["name"] == workspace.name
    assert info["role"] == workspace_service.ROLE_ADMIN
    assert info["memberCount"] == 1

    member = _user(db, email="u2@example.com")
    member.workspace_id = workspace.id
    member.role = workspace_service.ROLE_USER
    db.commit()

    member_info = workspace_service.workspace_info(db, member)
    assert member_info["memberCount"] == 2
    assert {m["role"] for m in member_info["members"]} == {"admin", "user"}


def test_personal_workspace_gets_a_unique_invite_code(db):
    one = workspace_service.resolve_for_user(db, _user(db, email="a@example.com"))
    two = workspace_service.resolve_for_user(db, _user(db, email="b@example.com"))

    assert len(one.invite_code) == 8
    assert one.invite_code != two.invite_code
    # 字符表去掉了易混淆字符:码会被人口抄、微信群转发
    assert not (set(one.invite_code) & set("01OI"))


def test_join_by_invite_code_makes_the_joiner_a_plain_user(db):
    """加入后是 user 角色:能管自己的私有文档，改不了团队共享文档。"""
    owner = _user(db, email="owner@example.com")
    workspace = workspace_service.resolve_for_user(db, owner)
    joiner = _user(db, email="joiner@example.com")
    workspace_service.resolve_for_user(db, joiner)

    joined = workspace_service.join_by_invite_code(db, joiner, workspace.invite_code)

    assert joined.id == workspace.id
    assert joiner.workspace_id == workspace.id
    assert joiner.role == workspace_service.ROLE_USER
    with pytest.raises(WorkspaceError):
        workspace_service.require_admin(joiner)


def test_invite_code_is_normalized_before_lookup(db):
    """码会被人手抄，大小写和首尾空格不该导致"邀请码无效"。"""
    owner = _user(db, email="owner@example.com")
    workspace = workspace_service.resolve_for_user(db, owner)
    joiner = _user(db, email="j@example.com")

    joined = workspace_service.join_by_invite_code(
        db, joiner, f"  {workspace.invite_code.lower()}  "
    )
    assert joined.id == workspace.id


def test_join_rejects_unknown_and_empty_codes(db):
    joiner = _user(db, email="j@example.com")
    with pytest.raises(WorkspaceError, match="邀请码无效"):
        workspace_service.join_by_invite_code(db, joiner, "NOSUCHCD")
    with pytest.raises(WorkspaceError, match="请输入邀请码"):
        workspace_service.join_by_invite_code(db, joiner, "   ")


def test_joining_the_same_workspace_twice_is_refused(db):
    owner = _user(db, email="owner@example.com")
    workspace = workspace_service.resolve_for_user(db, owner)
    with pytest.raises(WorkspaceError, match="已在该工作区"):
        workspace_service.join_by_invite_code(db, owner, workspace.invite_code)


def test_regenerating_the_code_invalidates_the_old_one(db):
    """泄露后的止损动作:旧码立即作废。"""
    owner = _user(db, email="owner@example.com")
    workspace = workspace_service.resolve_for_user(db, owner)
    old_code = workspace.invite_code

    new_code = workspace_service.regenerate_invite_code(db, owner)

    assert new_code != old_code
    joiner = _user(db, email="j@example.com")
    with pytest.raises(WorkspaceError, match="邀请码无效"):
        workspace_service.join_by_invite_code(db, joiner, old_code)


def test_only_admin_can_regenerate_the_code(db):
    owner = _user(db, email="owner@example.com")
    workspace = workspace_service.resolve_for_user(db, owner)
    joiner = _user(db, email="j@example.com")
    workspace_service.join_by_invite_code(db, joiner, workspace.invite_code)

    with pytest.raises(WorkspaceError, match="管理员"):
        workspace_service.regenerate_invite_code(db, joiner)


def test_invite_code_is_hidden_from_plain_users(db):
    """user 看不到码就不会转发给不该进来的人。"""
    owner = _user(db, email="owner@example.com")
    workspace = workspace_service.resolve_for_user(db, owner)
    joiner = _user(db, email="j@example.com")
    workspace_service.join_by_invite_code(db, joiner, workspace.invite_code)

    assert workspace_service.workspace_info(db, owner)["inviteCode"] == workspace.invite_code
    assert workspace_service.workspace_info(db, joiner)["inviteCode"] is None


def test_only_admin_can_rename_the_workspace(db):
    owner = _user(db, email="owner@example.com")
    workspace = workspace_service.resolve_for_user(db, owner)
    joiner = _user(db, email="j@example.com")
    workspace_service.join_by_invite_code(db, joiner, workspace.invite_code)

    renamed = workspace_service.rename(db, owner, "  研发中心  ")
    assert renamed.name == "研发中心"  # 去掉首尾空白

    with pytest.raises(WorkspaceError, match="管理员"):
        workspace_service.rename(db, joiner, "我说了算")


def test_rename_rejects_blank_names(db):
    owner = _user(db, email="owner@example.com")
    workspace_service.resolve_for_user(db, owner)
    with pytest.raises(WorkspaceError, match="不能为空"):
        workspace_service.rename(db, owner, "   ")


def test_legacy_member_role_is_treated_as_a_plain_user(db):
    """存量库里可能还有 role='member'(0007 那版的值)。

    不做数据迁移改写它:角色判断一律走 is_admin(),而它只认 'admin',
    所以任何非 admin 值都自动落到"一般用户"这一档——多一个等价值不会造成越权。
    """
    legacy = _user(db, email="legacy@example.com")
    legacy.role = workspace_service.ROLE_MEMBER_LEGACY
    db.commit()

    assert workspace_service.is_admin(legacy) is False
    with pytest.raises(WorkspaceError):
        workspace_service.require_admin(legacy)


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
