"""工作区(组织)服务:作用域、角色与可见性。

## 两层作用域

外层是**工作区**,内层是**可见性**(``Document.visibility``):

| 可见性 | 进谁的检索 | 出现在谁的列表 | 谁能增删 |
|---|---|---|---|
| ``workspace`` 共享 | 工作区全员 | 工作区全员 | 仅 admin |
| ``private`` 私有 | 仅上传者本人 | 本人 + **本工作区 admin** | 仅上传者本人 |

所以 ``user`` 角色**不是只读账号**——它只是不能改组织资产。个人临时资料进 private,
既不需要求 admin 代传,也不污染团队检索。

## "看得见"分成了两件事

私有文档那一行的第 2、3 列不一样,这是 2026-08-24 有意做的区分:

* **检索作用域**(``HybridRetriever._retrievable_by``)——哪些内容能被引用进回答。
  admin 在这里**没有**特权,别人的私有文档不会进他的检索。
* **管理作用域**(``listable_documents``)——admin 要知道自己这个空间里躺着什么。

合并这两件事都会坏一头:让 admin 检索到全员私有文档,他随口一问就可能引用到同事
的私有草稿,而界面上给上传者的承诺是"只有你能检索到";反过来不给 admin 列表可见性,
他就管不了自己空间里的存量——尤其是离职者留下的资料。

**"能看见"不等于"能改"**:admin 在列表里看得见别人的私有文档,但 ``require_can_modify``
仍然拒绝他删除。这一条是刻意的,理由见那个函数。

## 两级角色,不再扩

``admin`` 管共享文档与邀请码,``user`` 管自己的私有文档。不引入更细的权限矩阵——
两级之外的需求出现之前,复杂度都是负债。

角色名用 ``user`` 而不是 ``member``:调用方语汇里"一般用户"就是这个意思,而
``member`` 容易和"工作区成员列表"里的成员混起来(那个是**所有**人,含 admin)。

## 懒初始化

旧用户与 OAuth 新用户的 ``workspace_id`` 为空,第一次访问工作区相关功能时
``resolve_for_user`` 自动补建个人空间(admin)。注册、登录、OAuth 回调都不必
为此各写一遍。
"""
from __future__ import annotations

import secrets

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models import Document, User, Workspace

ROLE_ADMIN = "admin"
ROLE_USER = "user"
# 历史值。0007 那版用的是 member,存量库里可能还有这个字符串,语义等同 ROLE_USER。
# 不做数据迁移改写它:角色判断一律走 is_admin(),而它只认 ROLE_ADMIN,
# 所以任何非 admin 值都自动落到"一般用户"这一档——多一个等价值不会造成越权。
ROLE_MEMBER_LEGACY = "member"

VISIBILITY_WORKSPACE = "workspace"
VISIBILITY_PRIVATE = "private"
VISIBILITIES = (VISIBILITY_WORKSPACE, VISIBILITY_PRIVATE)

# 去掉易混淆字符(0/O、1/I),邀请码会被人口抄、微信群转发
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_INVITE_LENGTH = 8


class WorkspaceError(Exception):
    """业务错误,由路由层转成 4xx。message 面向最终用户。"""


def _new_invite_code() -> str:
    return "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(_INVITE_LENGTH))


def _unique_invite_code(db: Session) -> str:
    """碰撞概率 32^8 ≈ 1.1e12,重试几次足够;真撞了说明随机源坏了。"""
    for _ in range(5):
        code = _new_invite_code()
        if db.query(Workspace).filter(Workspace.invite_code == code).first() is None:
            return code
    raise WorkspaceError("邀请码生成失败，请重试")


def is_admin(user: User) -> bool:
    """唯一的角色判据。

    写成"等于 admin"而不是"不等于 user":那样任何拼错的角色值都会变成管理员,
    而这个方向的错误是**越权**。反过来拼错只会让人少个权限,能被投诉出来。
    """
    return user.role == ROLE_ADMIN


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


def join_by_invite_code(db: Session, user: User, code: str) -> Workspace:
    """凭邀请码加入工作区,成为 user 角色。

    加入是**换空间**而不是"多一个空间":``User.workspace_id`` 是单值外键。所以
    原来那个个人空间里的私有文档加入后就检索不到了——它们不会被删,但也不再
    出现在任何检索里。这是当前数据模型的限制,不是设计意图,调用方(路由层)
    必须把这件事告诉用户,而不是静默切换。

    真正的多空间归属需要一张成员关联表,那是另一件事。
    """
    normalized = code.strip().upper()
    if not normalized:
        raise WorkspaceError("请输入邀请码")
    workspace = (
        db.query(Workspace).filter(Workspace.invite_code == normalized).first()
    )
    if workspace is None:
        raise WorkspaceError("邀请码无效")
    if user.workspace_id == workspace.id:
        raise WorkspaceError("你已在该工作区中")

    user.workspace_id = workspace.id
    user.role = ROLE_USER
    db.commit()
    db.refresh(workspace)
    return workspace


def regenerate_invite_code(db: Session, user: User) -> str:
    """重置邀请码。旧码立即作废——泄露后的止损动作。"""
    require_admin(user)
    workspace = resolve_for_user(db, user)
    workspace.invite_code = _unique_invite_code(db)
    db.commit()
    return workspace.invite_code


# ---- 权限闸口 -------------------------------------------------------------
# 三个函数分别对应三个真实的判断点,不合并成一个带 flag 的通用函数:
# 合并之后调用点会写成 require_permission(user, doc, write=True),而读那一行
# 完全看不出它在拦什么。


def require_admin(user: User) -> None:
    """只有 admin 能做的事:改共享文档、改工作区名、重置邀请码。"""
    if not is_admin(user):
        raise WorkspaceError("仅工作区管理员可以执行此操作")


def rename(db: Session, user: User, name: str) -> Workspace:
    """改工作区名(仅 admin)。

    只开放改名这一项。删空间、转让 admin、移除成员都还没有——它们各自要先定
    语义(空间删了里面的共享文档归谁?最后一个 admin 能不能把自己降级?),
    而定不清楚的权限操作比没有这个操作更危险。
    """
    require_admin(user)
    cleaned = name.strip()
    if not cleaned:
        raise WorkspaceError("工作区名不能为空")
    workspace = resolve_for_user(db, user)
    workspace.name = cleaned[:100]
    db.commit()
    db.refresh(workspace)
    return workspace


def listable_documents(
    workspace_id: str,
    viewer_id: str | None,
    *,
    include_member_private: bool = False,
):
    """**管理列表**的可见范围,返回一个 SQLAlchemy 条件。

    这不是检索作用域。检索那一份在 ``HybridRetriever._retrievable_by``,两者
    2026-08-24 起**有意不同**:admin 在这里多看到成员的私有文档,在那里没有任何
    特权。两个名字刻意不像,免得调用点抓错一个——抓错的方向不对称,把管理条件
    用进检索就是"admin 的回答会引用同事的私有草稿"。

    ``include_member_private`` 由调用方按 ``is_admin(user)`` 传。默认 False,
    因为默认必须落在收紧那一侧:漏传只是 admin 少看到几篇(会被投诉),
    反过来则是把私有文档发给普通成员(不会被投诉,只会泄露)。

    ## admin 看到的是"当前成员"的私有文档,不是"workspace_id 等于本空间"的

    私有文档跟人走(见 ``HybridRetriever._retrievable_by``),所以它行上的
    ``workspace_id`` 是**上传时**那个空间,可能早就过期了。按那一列筛会同时错两头:
    一个人带着私有文档加入进来,新 admin 看不到它(而它确实在这台服务器上被索引);
    一个人带着私有文档离开,旧 admin 还在列表里看着它。

    所以这里按**所有者当前在不在这个工作区**判断。代价是多一个 IN 子查询,
    而收益是这个语义能自己保持正确——成员一走,他的私有文档就从这份列表里消失,
    不需要任何迁移动作。

    ``user_id`` 为 NULL 的私有文档因此**谁都列不到**,admin 也一样:子查询
    永远不会匹配 NULL。它们是删用户时 ``ondelete="SET NULL"`` 的产物,清理它们
    需要一条显式的运维动作,不该靠"admin 偶然在列表里看见"来兜底。
    """
    shared = (Document.visibility == VISIBILITY_WORKSPACE) & (
        Document.workspace_id == workspace_id
    )
    branches = [shared]
    if viewer_id:
        # 自己的私有文档,不限工作区——和检索那边一致
        branches.append(
            (Document.visibility == VISIBILITY_PRIVATE)
            & (Document.user_id == viewer_id)
        )
    if include_member_private:
        branches.append(
            (Document.visibility == VISIBILITY_PRIVATE)
            & Document.user_id.in_(
                select(User.id).where(User.workspace_id == workspace_id)
            )
        )
    if len(branches) == 1:
        return shared
    return or_(*branches)


def adopt_orphaned_documents(db: Session) -> int:
    """把没有所有者的私有文档收编成工作区共享文档,返回收编数量。

    ## 它修的是一个"谁都动不了"的状态

    ``Document.user_id`` 是 ``ondelete="SET NULL"``,所以删一个用户会把他的私有
    文档变成 ``(visibility=private, user_id=NULL)``。那种组合下:检索要求
    ``user_id == viewer``,NULL 永远不等;管理列表的 IN 子查询也永远不匹配 NULL;
    ``require_can_modify`` 走私有那一支,``NULL != user.id`` 于是拒掉所有人——
    **包括 admin**。结果是一份占着分块、算在容量里、谁都看不见也删不掉的孤儿。

    收编成共享之后 admin 能看见、能删,也能留着当团队资料。这是 2026-08-25 定的
    语义;此前 ``models.py`` 里写的是"需要 admin 显式处理",但当时没有任何接口
    能做那件事,所谓"显式处理"实际是永久滞留。

    ## 为什么是启动时扫,而不是删用户时转

    因为**没有删用户的接口**——``routers/`` 里一处都没有,所以今天删账号只能走
    SQL,而 SQL 不会触发任何应用层逻辑。真要加删用户端点时,直接调这个函数即可
    (它是幂等的);现在把它挂在启动上,是让唯一真实存在的入口能被兜住。

    代价说清楚:``workspace_id`` 用的是文档行上那一列,也就是**上传时**的工作区,
    而不是这个人离开时所在的工作区(那个信息随用户行一起没了)。人在被删之前换过
    工作区的话,文档会落回它被上传的那个空间。这是能拿到的最好的归属,而且是
    可解释的("它在哪儿传的就归哪儿")。

    ## 收编后 user_id 保持 NULL

    不是漏了填,是刻意留成标记:共享文档正常都带上传者 id,所以
    ``(visibility=workspace, user_id IS NULL)`` 这个组合就是"原主已离开"的标识,
    界面据此打一个"继承"徽章,不需要新加一列。
    """
    orphans = (
        db.query(Document)
        .filter(
            Document.visibility == VISIBILITY_PRIVATE,
            Document.user_id.is_(None),
        )
        .all()
    )
    if not orphans:
        return 0
    for document in orphans:
        document.visibility = VISIBILITY_WORKSPACE
    db.commit()

    # 索引与缓存必须跟着失效,否则收编只在数据库里生效:向量/BM25 索引按
    # scope 的 chunk_id 签名判断新鲜度,而这些块此前**不在任何 scope 的签名里**
    # (谁都检索不到),所以签名对每个受影响的工作区都变了,不主动清就要等下一次
    # 别的写操作碰巧把它清掉。语义缓存同理:收编前问过的问题不含这些内容。
    # 延迟导入:retrieval_index 与 semantic_cache 都会 import 到 models,
    # 顶层导入会绕成环。这个函数一次启动只调一遍,导入开销无所谓。
    from services.retrieval_index import invalidate_scope_indexes
    from services.semantic_cache import semantic_cache

    for workspace_id in {d.workspace_id for d in orphans if d.workspace_id}:
        invalidate_scope_indexes(workspace_id)
        # invalidate_user 这个名字有历史误导:它做的是按 scope 前缀清桶,
        # 而所有调用点传进去的都是 workspace_id(见 knowledge_service 那四处)。
        semantic_cache.invalidate_user(workspace_id)
    return len(orphans)


def resolve_upload_visibility(user: User, requested: str | None) -> str:
    """决定这次上传落在哪个可见性,顺便把越权挡掉。

    ``requested`` 为 None 时**不猜**,由调用方各自给默认值:知识库页面的默认是
    共享(那是它的用途),chat 附件的默认是私有(附一份文件问句话不该等于向团队
    发布)。在这里统一给默认值就会让其中一条变成错的。
    """
    if requested is None:
        raise WorkspaceError("未指定文档可见性")
    if requested not in VISIBILITIES:
        raise WorkspaceError(
            f"未知的可见性 {requested!r}，可用：{', '.join(VISIBILITIES)}"
        )
    if requested == VISIBILITY_WORKSPACE and not is_admin(user):
        raise WorkspaceError(
            "仅工作区管理员可以上传共享文档；你可以把它存为个人文档"
        )
    return requested


def require_can_modify(user: User, document: Document) -> None:
    """删除/修改单篇文档的闸口。

    共享文档要 admin;私有文档只有本人——**admin 也不例外**。理由是私有文档的
    语义是"只有我看得见",而一个能删掉别人私有资料的 admin 会让这个承诺失效。

    ## 于是 admin 有一个能看见但动不了的集合

    ``listable_documents(include_member_private=True)`` 让 admin 在列表里看到成员
    的私有文档,这里却拒绝他删除。这个组合是刻意的:知情权和处置权分开,前者用来
    管容量与合规,后者留给文档的主人。

    代价要说清楚——**目前没有任何接口能把这个集合清掉**。没有改可见性的端点,
    也没有"转让给 admin"的动作,所以离职者留下的私有文档(``user_id`` 变 NULL 之后
    连列表都进不去)只能靠数据库运维处理。真要补,该补的是一条显式的
    ``PATCH /documents/{id}/visibility``:由**所有者**把文档转成共享,然后 admin
    照共享文档的规则处置。给 admin 开一条直接删除的后门更省事,但那等于把上面
    那句承诺作废,而承诺一旦作废就没法再声明"私有"是什么意思。
    """
    if document.visibility == VISIBILITY_PRIVATE:
        if document.user_id != user.id:
            raise WorkspaceError("这是他人的个人文档，你没有权限操作")
        return
    require_admin(user)


def workspace_info(db: Session, user: User) -> dict:
    """前端展示用:工作区归属、成员列表与邀请码。"""
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
        "isAdmin": is_admin(user),
        "memberCount": len(members),
        "members": [
            {"id": m.id, "name": m.name or m.username or m.email, "role": m.role}
            for m in members
        ],
        # 邀请码只给 admin:user 看不到就不会转发给不该进来的人
        "inviteCode": workspace.invite_code if is_admin(user) else None,
    }
