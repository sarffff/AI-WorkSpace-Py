"""文档可见性:共享 / 私有的隔离与权限。

这个文件测的是**越权与泄露**,所以每一条都写成"另一个人看不见/改不了",而不是
"本人看得见"。后者是功能,前者是安全边界——功能坏了会被投诉,边界坏了不会。

## 四条防线,分别测

一份私有文档要泄露给别人,得同时穿过四层:

1. ``HybridRetriever._retrievable_by`` —— 检索的 SQL 过滤(唯一真正的边界)
2. ``workspace_service.listable_documents`` —— 管理列表的 SQL 过滤
3. ``_retrieve`` 末尾的 ``chunk_id in by_id`` —— Qdrant 只按 workspace 过滤,
   它命中别人的私有分块时靠这一句丢掉
4. 语义缓存的分桶键 —— 开 RAG 时带 viewer,否则 A 的答案会命中给 B

## 第 1、2 条对普通成员必须一致,对 admin 必须不一致

这是 2026-08-24 改掉的一条不变量,值得写清楚。原来两处结果集必须逐一相等,理由是
"列表里看得见、搜索搜不到"会让人以为检索坏了。现在 admin 的列表**故意**比他的检索
宽:他要能管自己空间里躺着什么,但别人的私有文档不该进他的回答。

所以断言变成两条:普通成员那边仍然相等(那个坑还在),admin 那边钉住差集恰好等于
"成员的私有文档",并且列表里每行的 ``retrievable`` 和真实检索结果对得上——
把差异做成一个字段,而不是留给界面猜。

## 为什么 viewer_id 的默认值是"只查共享"

漏传 ``viewer_id`` 的后果是少检索到自己的私有文档——能被投诉出来。
反过来(None 当成"不过滤")是越权,不会被投诉,只会泄露。所以默认必须是收紧那一侧,
而下面有一条断言钉住这个方向。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Document, DocumentChunk, User, Workspace
from services import workspace_service
from services.knowledge_service import KnowledgeService
from services.retriever import HybridRetriever
from services.workspace_service import WorkspaceError
from conftest import run

import models  # noqa: F401  确保所有表已注册


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()


@pytest.fixture()
def team(db):
    """一个工作区、一个 admin、两个普通用户。"""
    workspace = Workspace(name="团队", invite_code="TESTCODE")
    db.add(workspace)
    db.flush()
    people = {}
    for key, role in (("admin", "admin"), ("alice", "user"), ("bob", "user")):
        user = User(
            email=f"{key}@example.com",
            username=key,
            role=role,
            workspace_id=workspace.id,
        )
        db.add(user)
        people[key] = user
    db.commit()
    for user in people.values():
        db.refresh(user)
    return {"workspace": workspace, **people}


def _document(db, workspace_id, *, visibility, owner_id, name="doc.md", body="报销标准 500 元"):
    document = Document(
        name=name,
        size=len(body),
        content=body,
        workspace_id=workspace_id,
        user_id=owner_id,
        visibility=visibility,
        status="indexed",
        chunks=1,
    )
    db.add(document)
    db.flush()
    db.add(DocumentChunk(document_id=document.id, chunk_index=0, content=body))
    db.commit()
    db.refresh(document)
    return document


def _visible_chunk_ids(db, workspace_id, viewer_id):
    return HybridRetriever._load_chunk_ids(db, workspace_id, viewer_id)


def _listed(db, workspace_id, viewer_id, *, as_admin=False):
    """管理列表。``as_admin`` 就是路由层按 is_admin(user) 传的那个 flag。"""
    return run(
        KnowledgeService().get_documents(
            db, workspace_id, viewer_id=viewer_id, include_member_private=as_admin
        )
    )


def _listed_ids(db, workspace_id, viewer_id, *, as_admin=False):
    return {doc["id"] for doc in _listed(db, workspace_id, viewer_id, as_admin=as_admin)}


def _retrieved_doc_ids(db, workspace_id, viewer_id):
    """真实检索能碰到的文档 id,用来和列表的 retrievable 字段对账。"""
    chunk_ids = _visible_chunk_ids(db, workspace_id, viewer_id)
    return {
        row[0]
        for row in db.query(DocumentChunk.document_id)
        .filter(DocumentChunk.id.in_(chunk_ids))
        .all()
    }


# ========== 检索隔离(第 1 条防线) ==========


def test_private_document_is_invisible_to_another_user(team, db):
    """核心那一条:别人的私有文档不该进检索。"""
    workspace_id = team["workspace"].id
    private = _document(
        db, workspace_id, visibility="private", owner_id=team["alice"].id
    )

    assert _visible_chunk_ids(db, workspace_id, team["alice"].id), "本人应当看得见"
    assert _visible_chunk_ids(db, workspace_id, team["bob"].id) == []
    # admin 也不例外:私有的语义是"只有我看得见"
    assert _visible_chunk_ids(db, workspace_id, team["admin"].id) == []
    assert private.visibility == "private"


def test_shared_document_is_visible_to_everyone_including_plain_users(team, db):
    workspace_id = team["workspace"].id
    _document(db, workspace_id, visibility="workspace", owner_id=team["admin"].id)

    for key in ("admin", "alice", "bob"):
        assert _visible_chunk_ids(db, workspace_id, team[key].id), key


def test_viewer_sees_shared_plus_only_their_own_private(team, db):
    workspace_id = team["workspace"].id
    _document(db, workspace_id, visibility="workspace", owner_id=team["admin"].id, name="shared.md")
    _document(db, workspace_id, visibility="private", owner_id=team["alice"].id, name="alice.md")
    _document(db, workspace_id, visibility="private", owner_id=team["bob"].id, name="bob.md")

    assert len(_visible_chunk_ids(db, workspace_id, team["alice"].id)) == 2
    assert len(_visible_chunk_ids(db, workspace_id, team["bob"].id)) == 2
    assert len(_visible_chunk_ids(db, workspace_id, team["admin"].id)) == 1


def test_omitting_viewer_id_tightens_rather_than_opens(team, db):
    """默认方向必须是收紧。

    ``viewer_id=None`` 意思是"只要共享文档",不是"什么都能看"。None 当成不过滤
    是这类代码最典型的越权 bug,而它的表现是**功能正常**——所以只能靠断言钉住。
    """
    workspace_id = team["workspace"].id
    _document(db, workspace_id, visibility="private", owner_id=team["alice"].id)

    assert HybridRetriever._load_chunk_ids(db, workspace_id) == []


def test_private_document_with_no_owner_is_visible_to_nobody(team, db):
    """删用户时 ondelete="SET NULL" 会造出 user_id 为空的私有文档。

    它应当谁都检索不到——离职者留下的私有资料不该自动变成全员可见,
    而是需要 admin 显式处理。
    """
    workspace_id = team["workspace"].id
    _document(db, workspace_id, visibility="private", owner_id=None)

    for key in ("admin", "alice", "bob"):
        assert _visible_chunk_ids(db, workspace_id, team[key].id) == [], key


def test_visibility_does_not_leak_across_workspaces(db):
    """可见性是工作区**内部**的第二层,不能让它盖过工作区那一层。"""
    other = Workspace(name="别家", invite_code="OTHERCOD")
    mine = Workspace(name="我家", invite_code="MINECODE")
    db.add_all([other, mine])
    db.flush()
    outsider = User(email="x@example.com", username="x", role="admin", workspace_id=other.id)
    db.add(outsider)
    db.commit()
    _document(db, mine.id, visibility="workspace", owner_id=None)

    assert _visible_chunk_ids(db, other.id, outsider.id) == []


# ========== 列表与检索必须一致(第 2 条防线) ==========


def test_document_list_matches_what_retrieval_can_see(team, db):
    """对**普通成员**,两处过滤的结果集必须一致。

    不一致的表现是"列表里看得见、搜索搜不到"(或反过来),而两种都会让人以为
    检索坏了。admin 那边是有意不一致的,见下面两条。
    """
    workspace_id = team["workspace"].id
    shared = _document(db, workspace_id, visibility="workspace", owner_id=team["admin"].id, name="s.md")
    mine = _document(db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md")
    _document(db, workspace_id, visibility="private", owner_id=team["bob"].id, name="b.md")

    listed = _listed_ids(db, workspace_id, team["alice"].id)
    assert listed == {shared.id, mine.id}
    assert _retrieved_doc_ids(db, workspace_id, team["alice"].id) == listed


# ========== admin 的管理可见性(列表宽于检索) ==========


def test_admin_lists_every_members_private_document(team, db):
    """admin 要能知道自己空间里躺着什么,包括全体成员的个人文档。"""
    workspace_id = team["workspace"].id
    shared = _document(db, workspace_id, visibility="workspace", owner_id=team["admin"].id, name="s.md")
    a = _document(db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md")
    b = _document(db, workspace_id, visibility="private", owner_id=team["bob"].id, name="b.md")
    own = _document(db, workspace_id, visibility="private", owner_id=team["admin"].id, name="own.md")

    listed = _listed_ids(db, workspace_id, team["admin"].id, as_admin=True)
    assert listed == {shared.id, a.id, b.id, own.id}


def test_plain_user_still_cannot_list_other_peoples_private_documents(team, db):
    """这条特权只给 admin,而唯一的闸口在路由。

    ``listable_documents`` 自己不判角色——传 True 进去它就照办。这是有意的分层:
    条件构造和权限判断分开,免得服务层也要拿着 User 对象。代价是**路由那一句
    ``include_member_private=is_admin(user)`` 是真正的边界**,写错就是越权。
    所以这条测的是普通成员走正常路径(不传 flag)时看到什么,而 flag 的默认值
    由下一条钉住。
    """
    workspace_id = team["workspace"].id
    shared = _document(db, workspace_id, visibility="workspace", owner_id=team["admin"].id, name="s.md")
    mine = _document(db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md")
    _document(db, workspace_id, visibility="private", owner_id=team["bob"].id, name="b.md")

    assert _listed_ids(db, workspace_id, team["alice"].id) == {shared.id, mine.id}


def test_include_member_private_defaults_to_the_narrow_side(team, db):
    """漏传 flag 的后果必须是少看到,不是多看到。

    这个方向和 ``viewer_id`` 默认 None 是同一个道理:收紧那一侧的错误会被投诉,
    放开那一侧只会静默泄露。
    """
    workspace_id = team["workspace"].id
    other = _document(db, workspace_id, visibility="private", owner_id=team["bob"].id, name="b.md")

    # admin 本人,但没传 flag
    assert other.id not in _listed_ids(db, workspace_id, team["admin"].id)


def test_admin_can_list_but_cannot_retrieve_member_private_documents(team, db):
    """核心那一条:多出来的可见性**不进检索**。

    这是"看得见"和"会被引用进回答"的分界。串了的后果不是报错,而是 admin 随口
    一问就引用到同事的私有草稿——而上传界面对那个同事的承诺是"只有你能检索到"。
    """
    workspace_id = team["workspace"].id
    shared = _document(db, workspace_id, visibility="workspace", owner_id=team["admin"].id, name="s.md")
    alice_private = _document(
        db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md"
    )

    listed = _listed_ids(db, workspace_id, team["admin"].id, as_admin=True)
    retrieved = _retrieved_doc_ids(db, workspace_id, team["admin"].id)

    assert alice_private.id in listed
    assert retrieved == {shared.id}, "别人的私有文档不该进 admin 的检索"
    assert listed - retrieved == {alice_private.id}, "差集恰好是成员的私有文档"


def test_retrievable_field_matches_real_retrieval_for_admin(team, db):
    """列表每行的 ``retrievable`` 必须和真实检索对得上。

    这个字段是界面区分"可见"与"参与检索"的唯一依据。它算错了,admin 会看到一份
    标着"参与检索"的文档却问不出内容——那种不一致最难查,因为两边都不报错。
    """
    workspace_id = team["workspace"].id
    _document(db, workspace_id, visibility="workspace", owner_id=team["admin"].id, name="s.md")
    _document(db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md")
    _document(db, workspace_id, visibility="private", owner_id=team["admin"].id, name="own.md")

    documents = _listed(db, workspace_id, team["admin"].id, as_admin=True)
    retrieved = _retrieved_doc_ids(db, workspace_id, team["admin"].id)

    for doc in documents:
        assert doc["retrievable"] is (doc["id"] in retrieved), doc["name"]
    by_name = {doc["name"]: doc for doc in documents}
    assert by_name["a.md"]["retrievable"] is False
    assert by_name["own.md"]["retrievable"] is True
    assert by_name["s.md"]["retrievable"] is True


def test_list_carries_the_owner_name_so_admin_knows_who_to_ask(team, db):
    """看到一篇不能删的个人文档时,得知道该找谁。

    显示名不是新泄露的信息:``workspace_info.members`` 本来就返回全体成员的名字。
    """
    workspace_id = team["workspace"].id
    _document(db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md")

    by_name = {
        doc["name"]: doc
        for doc in _listed(db, workspace_id, team["admin"].id, as_admin=True)
    }
    assert by_name["a.md"]["ownerName"] == "alice"
    assert by_name["a.md"]["isOwn"] is False


def test_documents_with_no_owner_stay_out_of_the_admin_list(team, db):
    """``user_id`` 为 NULL 的私有文档 admin 也列不到。

    它们是删用户时 ondelete="SET NULL" 的产物。清理需要一条显式的运维动作,
    不该靠"admin 偶然在列表里看见"来兜底——而 IN 子查询永远不匹配 NULL,
    所以这个行为是免费得到的,这条断言只是把它钉住。
    """
    workspace_id = team["workspace"].id
    orphan = _document(db, workspace_id, visibility="private", owner_id=None, name="orphan.md")

    assert orphan.id not in _listed_ids(db, workspace_id, team["admin"].id, as_admin=True)
    assert _visible_chunk_ids(db, workspace_id, team["admin"].id) == []


def test_admin_privilege_does_not_reach_into_other_workspaces(team, db):
    """管理可见性是**这个工作区**的,不是全局的。

    判据是"所有者当前在不在这个工作区",所以另一个空间的私有文档进不来——
    哪怕这个人在自己空间里是 admin。
    """
    other = Workspace(name="别家", invite_code="OTHERCOD")
    db.add(other)
    db.flush()
    outsider = User(
        email="out@example.com", username="out", role="user", workspace_id=other.id
    )
    db.add(outsider)
    db.commit()
    theirs = _document(db, other.id, visibility="private", owner_id=outsider.id, name="x.md")

    listed = _listed_ids(db, team["workspace"].id, team["admin"].id, as_admin=True)
    assert theirs.id not in listed


def test_a_members_private_document_leaves_the_admin_list_when_they_leave(team, db):
    """判据是"当前成员",所以人一走文档就从列表里消失——不需要任何迁移动作。

    这正是不按 ``Document.workspace_id`` 筛的理由:那一列记的是**上传时**的空间,
    私有文档跟人走之后它就过期了。按它筛会让旧 admin 一直看着一篇已经不在他
    管辖范围内的文档。
    """
    workspace_id = team["workspace"].id
    alice_private = _document(
        db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md"
    )
    assert alice_private.id in _listed_ids(
        db, workspace_id, team["admin"].id, as_admin=True
    )

    elsewhere = Workspace(name="新东家", invite_code="NEWCODE1")
    db.add(elsewhere)
    db.flush()
    workspace_service.join_by_invite_code(db, team["alice"], "NEWCODE1")

    assert alice_private.id not in _listed_ids(
        db, workspace_id, team["admin"].id, as_admin=True
    )
    # 而她自己在新空间里仍然看得见、也仍然检索得到(私有文档跟人走)
    assert alice_private.id in _listed_ids(db, elsewhere.id, team["alice"].id)
    assert _visible_chunk_ids(db, elsewhere.id, team["alice"].id)


def test_admin_still_cannot_delete_a_document_they_can_now_see(team, db):
    """能看见 ≠ 能改。这两条断言合起来才是完整的语义。

    ``find_document`` 带 flag 时取得到,是为了让路由能给出诚实的 403 而不是 404
    (那一篇明明在他的列表里);``require_can_modify`` 紧接着拒掉。
    """
    workspace_id = team["workspace"].id
    alice_private = _document(
        db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md"
    )
    service = KnowledgeService()

    found = run(
        service.find_document(
            db,
            alice_private.id,
            workspace_id,
            viewer_id=team["admin"].id,
            include_member_private=True,
        )
    )
    assert found is not None, "取得到,才能报出诚实的 403"
    with pytest.raises(WorkspaceError, match="他人的个人文档"):
        workspace_service.require_can_modify(team["admin"], found)

    # 且服务层那一道(不带 flag)仍然当它不存在:绕过路由也删不掉
    assert (
        run(service.delete_document(db, alice_private.id, workspace_id, team["admin"].id))
        is False
    )
    assert db.query(Document).filter(Document.id == alice_private.id).first() is not None


def test_list_marks_ownership_and_visibility_for_the_ui(team, db):
    """界面要据此显示"共享/个人"并决定删除按钮给不给点。

    ``isOwn`` 由后端算而不是前端比 user_id:前端手上不一定有当前用户 id,
    而这个判断错了就是一个能点但会 403 的按钮。
    """
    workspace_id = team["workspace"].id
    _document(db, workspace_id, visibility="workspace", owner_id=team["admin"].id, name="s.md")
    _document(db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md")

    by_name = {
        doc["name"]: doc
        for doc in run(
            KnowledgeService().get_documents(db, workspace_id, viewer_id=team["alice"].id)
        )
    }
    assert by_name["s.md"]["visibility"] == "workspace"
    assert by_name["s.md"]["isOwn"] is False
    assert by_name["a.md"]["visibility"] == "private"
    assert by_name["a.md"]["isOwn"] is True


# ========== 离职者留下的无主文档(收编) ==========
#
# ondelete="SET NULL" 造出的 (private, user_id=NULL) 是个谁都动不了的状态:
# 检索要 user_id == viewer(NULL 不等),管理列表的 IN 子查询不匹配 NULL,
# require_can_modify 走私有那一支于是拒掉所有人——包括 admin。
# adopt_orphaned_documents 把它们转成共享,让 admin 能看见并自己决定。


def test_orphaned_private_document_is_invisible_to_everyone_before_adoption(team, db):
    """先钉住那个坏状态本身,收编才有意义可言。"""
    workspace_id = team["workspace"].id
    orphan = _document(db, workspace_id, visibility="private", owner_id=None, name="离职者.md")

    assert orphan.id not in _listed_ids(db, workspace_id, team["admin"].id, as_admin=True)
    assert orphan.id not in _listed_ids(db, workspace_id, team["alice"].id)
    assert _visible_chunk_ids(db, workspace_id, team["admin"].id) == []
    # 连 admin 都删不掉:走私有那一支,NULL != admin.id
    with pytest.raises(WorkspaceError, match="他人的个人文档"):
        workspace_service.require_can_modify(team["admin"], orphan)


def test_adoption_turns_orphans_into_shared_documents(team, db):
    """收编之后 admin 看得见、检索得到、也删得掉。"""
    workspace_id = team["workspace"].id
    orphan = _document(db, workspace_id, visibility="private", owner_id=None, name="离职者.md")

    assert workspace_service.adopt_orphaned_documents(db) == 1

    db.refresh(orphan)
    assert orphan.visibility == "workspace"
    assert orphan.id in _listed_ids(db, workspace_id, team["admin"].id, as_admin=True)
    # 共享文档全员可见,普通成员也一样
    assert orphan.id in _listed_ids(db, workspace_id, team["alice"].id)
    assert _visible_chunk_ids(db, workspace_id, team["alice"].id)
    # 现在按共享文档的规则处置:admin 能删,普通成员不能
    workspace_service.require_can_modify(team["admin"], orphan)
    with pytest.raises(WorkspaceError, match="管理员"):
        workspace_service.require_can_modify(team["alice"], orphan)


def test_adoption_keeps_user_id_null_as_the_inherited_marker(team, db):
    """``user_id`` 留成 NULL 是标记,不是漏填。

    正常共享文档都带上传者 id,所以 (workspace, user_id IS NULL) 这个组合就是
    "原主已离开"。界面据此打「继承」徽章,省掉一列和一次迁移。
    """
    workspace_id = team["workspace"].id
    orphan = _document(db, workspace_id, visibility="private", owner_id=None, name="离职者.md")
    normal = _document(
        db, workspace_id, visibility="workspace", owner_id=team["admin"].id, name="手册.md"
    )
    workspace_service.adopt_orphaned_documents(db)

    by_name = {
        doc["name"]: doc
        for doc in _listed(db, workspace_id, team["admin"].id, as_admin=True)
    }
    assert by_name["离职者.md"]["inherited"] is True
    assert by_name["手册.md"]["inherited"] is False
    db.refresh(orphan)
    db.refresh(normal)
    assert orphan.user_id is None
    assert normal.user_id == team["admin"].id


def test_adoption_leaves_owned_private_documents_alone(team, db):
    """只碰无主的那些。碰错了就是把在职员工的私有文档公开给全工作区。"""
    workspace_id = team["workspace"].id
    alice_private = _document(
        db, workspace_id, visibility="private", owner_id=team["alice"].id, name="a.md"
    )

    assert workspace_service.adopt_orphaned_documents(db) == 0

    db.refresh(alice_private)
    assert alice_private.visibility == "private"
    assert alice_private.id not in _listed_ids(db, workspace_id, team["bob"].id)


def test_adoption_is_idempotent(team, db):
    """挂在启动上,所以每次重启都会跑一遍;第二遍必须什么都不做。"""
    workspace_id = team["workspace"].id
    _document(db, workspace_id, visibility="private", owner_id=None, name="离职者.md")

    assert workspace_service.adopt_orphaned_documents(db) == 1
    assert workspace_service.adopt_orphaned_documents(db) == 0
    assert workspace_service.adopt_orphaned_documents(db) == 0


def test_adoption_routes_by_the_documents_own_workspace(team, db):
    """归属按文档行上的 ``workspace_id``,也就是**上传时**那个空间。

    离开时所在的空间随用户行一起没了,拿不到。所以规则是"在哪儿传的就归哪儿",
    这条断言把它钉住——两个空间各有一份孤儿,收编后不能串。
    """
    other = Workspace(name="别家", invite_code="OTHERCOD")
    db.add(other)
    db.flush()
    outsider = User(
        email="out@example.com", username="out", role="admin", workspace_id=other.id
    )
    db.add(outsider)
    db.commit()

    here = _document(
        db, team["workspace"].id, visibility="private", owner_id=None, name="这边.md"
    )
    there = _document(db, other.id, visibility="private", owner_id=None, name="那边.md")

    assert workspace_service.adopt_orphaned_documents(db) == 2

    assert here.id in _listed_ids(db, team["workspace"].id, team["admin"].id, as_admin=True)
    assert there.id not in _listed_ids(
        db, team["workspace"].id, team["admin"].id, as_admin=True
    )
    assert there.id in _listed_ids(db, other.id, outsider.id, as_admin=True)


def test_adoption_invalidates_indexes_for_affected_workspaces(team, db):
    """收编必须让索引失效,否则只在数据库里生效。

    孤儿块此前**不在任何 scope 的签名里**(谁都检索不到),收编后签名变了。不主动
    清的现象是"admin 在列表里看到文档,但问不出里面的内容",而且重启就好——
    这类"重启才好"的 bug 最难查。

    断言走 ``indexes_fresh`` 而不是 ``get_scope_indexes``:后者用 setdefault,
    永远返回对象、永远不是 None,拿它断言等于什么都没测(我第一版就写错了)。
    """
    from services import retrieval_index

    workspace_id = team["workspace"].id
    _document(db, workspace_id, visibility="private", owner_id=None, name="离职者.md")

    # 先把 admin 的桶建成"新鲜":签名此刻**不含**孤儿块
    scope = retrieval_index.scope_key(workspace_id, team["admin"].id)
    before = HybridRetriever._load_chunk_ids(db, workspace_id, team["admin"].id)
    stale_signature = retrieval_index.signature_from_ids(before)
    bundle = retrieval_index.get_scope_indexes(scope)
    bundle.vector.build_if_stale([], stale_signature)
    bundle.bm25.build_if_stale([], stale_signature)
    assert retrieval_index.indexes_fresh(scope, stale_signature)

    workspace_service.adopt_orphaned_documents(db)

    # 桶被清掉了:同一个签名不再是新鲜的
    assert not retrieval_index.indexes_fresh(scope, stale_signature)
    # 而且签名真的变了——孤儿块进了 admin 的可检索集合
    after = HybridRetriever._load_chunk_ids(db, workspace_id, team["admin"].id)
    assert len(after) == len(before) + 1
    assert retrieval_index.signature_from_ids(after) != stale_signature


# ========== 上传权限 ==========


def test_plain_user_can_upload_private_but_not_shared(team, db):
    """这就是"一般员工能不能上传"的答案:能,但只能传给自己。"""
    resolve = workspace_service.resolve_upload_visibility

    assert resolve(team["alice"], "private") == "private"
    with pytest.raises(WorkspaceError, match="管理员"):
        resolve(team["alice"], "workspace")


def test_admin_can_upload_both(team, db):
    resolve = workspace_service.resolve_upload_visibility
    assert resolve(team["admin"], "workspace") == "workspace"
    assert resolve(team["admin"], "private") == "private"


def test_unknown_visibility_is_rejected(team, db):
    with pytest.raises(WorkspaceError, match="未知的可见性"):
        workspace_service.resolve_upload_visibility(team["admin"], "public")


def test_visibility_must_be_explicit(team, db):
    """不在这里给默认值:知识库页面默认共享、chat 附件默认私有,
    统一给默认会让其中一条变成错的。"""
    with pytest.raises(WorkspaceError, match="未指定"):
        workspace_service.resolve_upload_visibility(team["admin"], None)


# ========== 删除权限 ==========


def test_plain_user_can_delete_own_private_document(team, db):
    document = _document(
        db, team["workspace"].id, visibility="private", owner_id=team["alice"].id
    )
    workspace_service.require_can_modify(team["alice"], document)  # 不抛即通过


def test_nobody_else_can_delete_someone_elses_private_document(team, db):
    """**admin 也不行。**

    私有文档的语义是"只有我看得见",而一个能删掉别人私有资料的 admin 会让这个
    承诺失效。admin 要清理离职者的私有文档时,正确做法是先转成共享——那是一个
    显式动作,有痕迹。
    """
    document = _document(
        db, team["workspace"].id, visibility="private", owner_id=team["alice"].id
    )
    for key in ("bob", "admin"):
        with pytest.raises(WorkspaceError, match="他人的个人文档"):
            workspace_service.require_can_modify(team[key], document)


def test_shared_document_requires_admin_to_delete(team, db):
    document = _document(
        db, team["workspace"].id, visibility="workspace", owner_id=team["admin"].id
    )
    workspace_service.require_can_modify(team["admin"], document)
    with pytest.raises(WorkspaceError, match="管理员"):
        workspace_service.require_can_modify(team["alice"], document)


# ========== 去重范围 ==========


def test_same_content_does_not_dedup_across_two_users_private_copies(team, db):
    """两个人各自上传同一份文件,不能让后一个人拿到前一个人的私有文档。

    去重键加 visibility 之前它就是 (工作区, 哈希),所以 bob 上传 alice 传过的
    同一份文件会直接返回 alice 那一行——既是越权也是错误的复用。
    """
    service = KnowledgeService()
    payload = b"# workspace policy\n\nreimbursement limit is 500\n"

    alice_doc, alice_dup = run(
        service.create_document(
            db, "policy.md", payload, team["workspace"].id,
            uploader_id=team["alice"].id, visibility="private",
        )
    )
    bob_doc, bob_dup = run(
        service.create_document(
            db, "policy.md", payload, team["workspace"].id,
            uploader_id=team["bob"].id, visibility="private",
        )
    )

    assert alice_dup is False
    assert bob_dup is False, "bob 命中了 alice 的私有文档"
    assert alice_doc.id != bob_doc.id
    assert bob_doc.user_id == team["bob"].id


def test_same_user_uploading_twice_still_dedups(team, db):
    """去重本身不能坏掉:重复文档会占 top_k 的多个席位。"""
    service = KnowledgeService()
    payload = b"# policy\n\nlimit 500\n"
    kwargs = dict(
        workspace_id=team["workspace"].id,
        uploader_id=team["alice"].id,
        visibility="private",
    )
    first, _ = run(service.create_document(db, "p.md", payload, **kwargs))
    second, duplicate = run(service.create_document(db, "p.md", payload, **kwargs))

    assert duplicate is True
    assert second.id == first.id


def test_shared_and_private_copies_of_the_same_content_coexist(team, db):
    """同一份内容既是团队资产又是某人的私有副本,是两篇不同归属的文档。"""
    service = KnowledgeService()
    payload = b"# policy\n\nlimit 500\n"

    shared, _ = run(
        service.create_document(
            db, "p.md", payload, team["workspace"].id,
            uploader_id=team["admin"].id, visibility="workspace",
        )
    )
    private, duplicate = run(
        service.create_document(
            db, "p.md", payload, team["workspace"].id,
            uploader_id=team["alice"].id, visibility="private",
        )
    )
    assert duplicate is False
    assert private.id != shared.id


# ========== 私有文档跟人走(换工作区) ==========
# User.workspace_id 是单值外键,所以"加入"等于"离开"。个人文档的归属是人而不是
# 空间,否则换一次工作区就再也搜不到自己传过的东西——而那不是边缘情况,是加入的
# 必然后果。


def _second_workspace(db):
    workspace = Workspace(name="新团队", invite_code="SECONDCD")
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace


def test_private_documents_follow_the_user_across_workspaces(team, db):
    """在 A 空间传的私有文档,加入 B 空间之后仍然检索得到。

    实现点是 ``_retrievable_by`` 里 ``workspace_id`` **只约束共享那一支**。
    把它放回外层当 AND 条件就会让这条断言失败,而症状只是"换空间后搜不到
    自己的东西",不报任何错。
    """
    alice = team["alice"]
    old_workspace = team["workspace"].id
    _document(db, old_workspace, visibility="private", owner_id=alice.id, name="mine.md")

    # 加入新空间
    new_workspace = _second_workspace(db)
    alice.workspace_id = new_workspace.id
    alice.role = workspace_service.ROLE_USER
    db.commit()

    assert _visible_chunk_ids(db, new_workspace.id, alice.id), "换空间后搜不到自己的私有文档"
    assert _listed_ids(db, new_workspace.id, alice.id), "列表里也应当还在"


def test_shared_documents_do_not_follow_the_user(team, db):
    """共享文档留在原空间:它是组织资产,不该被带走。"""
    alice = team["alice"]
    _document(
        db, team["workspace"].id, visibility="workspace",
        owner_id=team["admin"].id, name="policy.md",
    )
    new_workspace = _second_workspace(db)
    alice.workspace_id = new_workspace.id
    db.commit()

    assert _visible_chunk_ids(db, new_workspace.id, alice.id) == []
    assert _listed_ids(db, new_workspace.id, alice.id) == set()


def test_joining_gives_access_to_the_new_workspace_shared_documents(team, db):
    """加入之后能检索到 = 新空间的共享 + 自己的私有。这就是要的语义。"""
    alice = team["alice"]
    _document(
        db, team["workspace"].id, visibility="private", owner_id=alice.id, name="mine.md"
    )
    new_workspace = _second_workspace(db)
    _document(
        db, new_workspace.id, visibility="workspace", owner_id=None, name="theirs.md"
    )
    alice.workspace_id = new_workspace.id
    db.commit()

    listed = {
        doc["name"]
        for doc in run(
            KnowledgeService().get_documents(db, new_workspace.id, viewer_id=alice.id)
        )
    }
    assert listed == {"mine.md", "theirs.md"}


def test_a_users_private_document_stays_invisible_to_the_new_workspace(team, db):
    """带进来的私有文档只有本人搜得到,新空间的 admin 也看不见。"""
    alice = team["alice"]
    _document(
        db, team["workspace"].id, visibility="private", owner_id=alice.id, name="mine.md"
    )
    new_workspace = _second_workspace(db)
    stranger = User(
        email="boss@example.com", username="boss", role="admin",
        workspace_id=new_workspace.id,
    )
    db.add(stranger)
    alice.workspace_id = new_workspace.id
    db.commit()

    assert _visible_chunk_ids(db, new_workspace.id, stranger.id) == []


def test_own_private_document_is_deletable_after_switching_workspaces(team, db):
    """能看见就必须能删。

    ``find_document`` 如果只按当前 workspace_id 查,这一篇会找不到 → 404,
    于是出现"列表里有、删不掉"。那比看不见更让人困惑。
    """
    alice = team["alice"]
    document = _document(
        db, team["workspace"].id, visibility="private", owner_id=alice.id
    )
    new_workspace = _second_workspace(db)
    alice.workspace_id = new_workspace.id
    db.commit()

    service = KnowledgeService()
    found = run(service.find_document(db, document.id, new_workspace.id, alice.id))
    assert found is not None
    workspace_service.require_can_modify(alice, found)
    assert run(service.delete_document(db, document.id, new_workspace.id, alice.id))


# ========== 索引与缓存的分桶(第 3、4 条防线) ==========


def test_index_buckets_are_per_viewer():
    """索引按(工作区, 查看者)分桶。

    共用一份索引仍然"安全"(``chunk_id in by_id`` 会滤掉),但那是把隔离押在一个
    后置过滤上,而且别人的私有分块会占掉 per-channel 的候选席位。
    """
    from services.retrieval_index import scope_key

    assert scope_key("ws", "alice") != scope_key("ws", "bob")
    # 不传查看者时退回纯工作区键——那是"只检索共享文档"的语义
    assert scope_key("ws") == "ws"


def test_invalidating_a_workspace_clears_every_viewer_bucket():
    """admin 传了新共享文档,所有人的桶都要失效。

    只 pop 精确键会让带 viewer 的桶继续用旧索引,症状是"admin 传了文档,
    别人搜不到,重启才好"。
    """
    from services.retrieval_index import (
        _indexes,
        get_scope_indexes,
        invalidate_scope_indexes,
    )

    for key in ("ws", "ws|alice", "ws|bob", "other", "other|alice"):
        get_scope_indexes(key)
    invalidate_scope_indexes("ws")

    remaining = set(_indexes)
    assert "ws" not in remaining and "ws|alice" not in remaining
    assert {"other", "other|alice"} <= remaining, "不该动别的工作区"
    for key in ("other", "other|alice"):
        _indexes.pop(key, None)


def test_invalidating_by_viewer_reaches_buckets_in_any_workspace():
    """私有文档增删要按**人**清索引,不能只按工作区。

    桶键是 ``<他当前所在工作区>|<他>``,而私有文档的 workspace_id 可能是上一个
    空间。只按工作区前缀清的症状是"删了自己的个人文档,当前会话里还搜得到"。
    """
    from services.retrieval_index import (
        _indexes,
        get_scope_indexes,
        invalidate_viewer_indexes,
    )

    for key in ("wsA|alice", "wsB|alice", "wsB|bob", "wsB"):
        get_scope_indexes(key)
    invalidate_viewer_indexes("alice")

    remaining = set(_indexes)
    assert "wsA|alice" not in remaining and "wsB|alice" not in remaining
    assert {"wsB|bob", "wsB"} <= remaining, "不该动别人的桶"
    for key in ("wsB|bob", "wsB"):
        _indexes.pop(key, None)


def test_semantic_cache_invalidation_by_viewer_spans_workspaces():
    from services.semantic_cache import CacheEntry, _Store

    store = _Store()
    entry = CacheEntry(
        question="q", answer="a", model="m", use_rag=True,
        prompt_ref="v1", embedding=[1.0], created_at=1.0,
    )
    store.add("wsA|alice", entry, max_entries=10)
    store.add("wsB|alice", entry, max_entries=10)
    store.add("wsB|bob", entry, max_entries=10)

    # alice 在两个空间各有一条,都要清掉
    assert store.drop_by_viewer("alice") == 2
    assert store.bucket("wsA|alice") == []
    assert store.bucket("wsB|alice") == []
    # bob 的那条必须还在——反向断言，否则一个"清空所有桶"的实现也能通过
    assert len(store.bucket("wsB|bob")) == 1


def test_semantic_cache_buckets_do_not_leak_across_users():
    """开 RAG 时缓存必须带 viewer,否则 A 的答案会命中给 B。

    ``semantic_cache`` 自己的文档第一条就写着"跨用户命中就是数据泄露,不是优化";
    工作区分桶是共享知识库那一轮加的,私有文档让它的前提不再成立。
    """
    from services.semantic_cache import CacheEntry, _Store

    store = _Store()
    entry = CacheEntry(
        question="报销上限", answer="500", model="m", use_rag=True,
        prompt_ref="v1", embedding=[1.0], created_at=1.0,
    )
    store.add("ws|alice", entry, max_entries=10)

    assert store.bucket("ws|bob") == []
    assert len(store.bucket("ws|alice")) == 1

    # 知识库变化要清掉该工作区**所有**桶,含按用户细分的
    removed = store.drop("ws")
    assert removed == 1
    assert store.bucket("ws|alice") == []
