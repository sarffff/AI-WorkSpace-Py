"""成员管理：名册可见性、角色变更、移除。

此前这个模块能做的事是：看空间、凭邀请码加入、改名、重置邀请码。也就是说
**admin 看不到自己空间里有谁的管理信息，不能改谁的角色，更不能把人移出去**——
邀请码是个单向阀，进得来出不去。员工离职之后他的账号仍然在工作区里、仍然能检索
全部共享文档，而产品内没有任何办法处理，只能改库。

两条不变量是这一组的重点：

1. **最后一个 admin 既不能降级也不能被移除。** 这个产品里没有超级管理员，
   没有 admin 的空间是不可恢复的（改不了名、重置不了邀请码、再没人能提拔谁）。
2. **移除自己不走这条路。** 那是"退出工作区"，语义不同。

第三条不太显然、但值得单独钉：**移除成员不需要搬他的私有文档**。
``listable_documents`` 判的是所有者当前在不在这个工作区，不是文档行上的
``workspace_id``，所以人一走那些文档就自动从旧 admin 的列表里消失。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Document, User
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


def _join(db, workspace, email, role="user") -> User:
    """把一个人放进工作区。

    直接建行而不走邀请码：这一组测的是成员管理，让每条用例先跑一遍加入流程
    会让失败原因不唯一。
    """
    member = _user(db, email=email)
    member.workspace_id = workspace.id
    member.role = role
    db.commit()
    db.refresh(member)
    return member


def _doc(db, *, owner, workspace_id, visibility, name="x.md") -> Document:
    document = Document(
        name=name,
        size=10,
        user_id=owner.id,
        workspace_id=workspace_id,
        visibility=visibility,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


# ========== 名册与管理字段 ==========


def test_管理字段只给admin_成员看不到别人的邮箱(db):
    """名册全员可见，email 只给 admin——给全员看等于把同事邮箱发给所有人。"""
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    _join(db, workspace, "u2@example.com")

    as_admin = workspace_service.workspace_info(db, admin)
    assert all("email" in row for row in as_admin["members"])
    assert any(row["isSelf"] for row in as_admin["members"])

    member = db.query(User).filter(User.email == "u2@example.com").first()
    as_member = workspace_service.workspace_info(db, member)
    # 名册还在：同一个空间里的人知道彼此是谁不是特权
    assert as_member["memberCount"] == 2
    # 但管理字段一个都没有
    assert all("email" not in row for row in as_member["members"])
    assert all("isSelf" not in row for row in as_member["members"])


def test_admin排在前面且带adminCount(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    _join(db, workspace, "z@example.com")
    _join(db, workspace, "y@example.com", role="admin")

    info = workspace_service.workspace_info(db, admin)
    assert [row["role"] for row in info["members"]] == ["admin", "admin", "user"]
    # adminCount 是后端拒绝的依据，前端不该自己再数一遍
    assert info["adminCount"] == 2


# ========== 角色变更 ==========


def test_提升成员为管理员(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    member = _join(db, workspace, "u2@example.com")

    result = workspace_service.set_member_role(db, admin, member.id, "admin")

    assert result["role"] == "admin"
    db.refresh(member)
    assert workspace_service.is_admin(member)


def test_最后一个管理员不能把自己降级(db):
    """没有 admin 的空间改不了名、重置不了邀请码、再没人能提拔谁——不可恢复。"""
    admin = _user(db)
    workspace_service.resolve_for_user(db, admin)

    with pytest.raises(WorkspaceError, match="最后一个管理员"):
        workspace_service.set_member_role(db, admin, admin.id, "user")

    db.refresh(admin)
    assert workspace_service.is_admin(admin)


def test_有第二个管理员时可以把自己降级(db):
    """自己降级和别人降级用同一条规则，不特例。"""
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    _join(db, workspace, "u2@example.com", role="admin")

    workspace_service.set_member_role(db, admin, admin.id, "user")

    db.refresh(admin)
    assert not workspace_service.is_admin(admin)


def test_普通成员不能改角色(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    member = _join(db, workspace, "u2@example.com")

    with pytest.raises(WorkspaceError, match="仅工作区管理员"):
        workspace_service.set_member_role(db, member, admin.id, "user")


def test_改角色只认两个值且不能原地重复设(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    member = _join(db, workspace, "u2@example.com")

    with pytest.raises(WorkspaceError, match="只能是"):
        workspace_service.set_member_role(db, admin, member.id, "owner")
    with pytest.raises(WorkspaceError, match="已经是这个角色"):
        workspace_service.set_member_role(db, admin, member.id, "user")


# ========== 移除 ==========


def test_移除成员之后他不再属于这个工作区(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    member = _join(db, workspace, "leaver@example.com")

    result = workspace_service.remove_member(db, admin, member.id)

    assert result["id"] == member.id
    db.refresh(member)
    assert member.workspace_id is None
    assert workspace_service.workspace_info(db, admin)["memberCount"] == 1


def test_移除成员不搬私有文档而是靠成员判据自动生效(db):
    """这一条钉的是"不需要迁移"这个结论本身。

    我第一版打算写一个把 ``workspace_id`` 搬过去的迁移，而那正是
    ``listable_documents`` 当初改掉的写法（按行上的 workspace_id 筛会两头都错）。
    """
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    member = _join(db, workspace, "leaver@example.com")
    private = _doc(
        db,
        owner=member,
        workspace_id=workspace.id,
        visibility=workspace_service.VISIBILITY_PRIVATE,
        name="草稿.md",
    )

    before = workspace_service.listable_documents(
        workspace.id, admin.id, include_member_private=True
    )
    assert db.query(Document).filter(before).count() == 1

    workspace_service.remove_member(db, admin, member.id)

    # 文档行一个字都没动
    db.refresh(private)
    assert private.workspace_id == workspace.id
    assert private.user_id == member.id
    assert private.visibility == workspace_service.VISIBILITY_PRIVATE
    # 但旧 admin 已经列不到它了
    after = workspace_service.listable_documents(
        workspace.id, admin.id, include_member_private=True
    )
    assert db.query(Document).filter(after).count() == 0
    # 而本人在任何空间里都还看得到自己的私有文档（自己那一支不限工作区）
    own = workspace_service.listable_documents("another-workspace", member.id)
    assert db.query(Document).filter(own).count() == 1


def test_共享文档不跟着人走(db):
    """共享文档是组织资产，不是个人财产。"""
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    member = _join(db, workspace, "leaver@example.com")
    _doc(
        db,
        owner=member,
        workspace_id=workspace.id,
        visibility=workspace_service.VISIBILITY_WORKSPACE,
        name="团队手册.md",
    )

    workspace_service.remove_member(db, admin, member.id)

    condition = workspace_service.listable_documents(workspace.id, admin.id)
    assert db.query(Document).filter(condition).count() == 1


def test_不能移除自己(db):
    """那是"退出工作区"——用这个接口把自己踢掉会让下一次请求静默补建个人空间。"""
    admin = _user(db)
    workspace_service.resolve_for_user(db, admin)

    with pytest.raises(WorkspaceError, match="不能移除自己"):
        workspace_service.remove_member(db, admin, admin.id)


def test_移除一个管理员之后调用者自己还在_所以永远剩得下管理员(db):
    """钉的是一条**推理**，而不是一句判断。

    ``remove_member`` 里没有"最后一个 admin 不能被移除"的守卫，因为它不可达：
    调用者必须是 admin，而自己又移不掉，所以移除之后至少剩调用者。第一版写了那个
    守卫，反向验证时发现它一条用例都覆盖不到（把 ``_admin_count`` 改成永远返回 99，
    只有降级那条转红）——不可达的守卫读起来像防线，实际是永远为假的条件。

    这条不变量因此挂在"不能移除自己"那一句上。以后加"退出工作区"（允许自己走）
    时，那个新入口必须自己带 admin 计数判断。
    """
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    second = _join(db, workspace, "admin2@example.com", role="admin")

    # 移掉另一个 admin：允许，因为调用者自己还是 admin
    workspace_service.remove_member(db, admin, second.id)

    db.refresh(second)
    assert second.workspace_id is None
    info = workspace_service.workspace_info(db, admin)
    # 关键断言：空间里仍然有 admin。这是"不能移除自己"的直接后果
    assert info["adminCount"] == 1
    assert info["memberCount"] == 1


def test_移除不在本工作区的人被拒(db):
    """错误消息不区分"不存在"和"在别的空间"——后者会把别人的归属漏出去。"""
    admin = _user(db)
    workspace_service.resolve_for_user(db, admin)
    outsider = _user(db, email="out@example.com")
    workspace_service.resolve_for_user(db, outsider)

    with pytest.raises(WorkspaceError, match="不在这个工作区"):
        workspace_service.remove_member(db, admin, outsider.id)
    with pytest.raises(WorkspaceError, match="不在这个工作区"):
        workspace_service.set_member_role(db, admin, outsider.id, "admin")


def test_普通成员不能移除任何人(db):
    admin = _user(db)
    workspace = workspace_service.resolve_for_user(db, admin)
    member = _join(db, workspace, "u2@example.com")
    other = _join(db, workspace, "u3@example.com")

    with pytest.raises(WorkspaceError, match="仅工作区管理员"):
        workspace_service.remove_member(db, member, other.id)
