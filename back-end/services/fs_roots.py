"""本机文件夹授权与路径沙箱。

文件系统工具的全部安全性压在这个模块的 ``resolve_within_roots`` 上。工具处理器
本身不做任何路径判断——它们拿到的是这个函数返回的绝对路径，或者一个异常。

## 为什么沙箱是唯一的边界

路径是**模型写的**。模型可能在复述它刚读到的文件内容、刚抓的网页、或者知识库
里某个分块里夹带的字符串。``read_attachment`` 那边已经踩过这个形状
（见 ``workspace_tools.resolve_upload_path``）：少了前缀校验，``../../.env``
被读到一次就是一次凭据泄露，而且它会以一段看起来完全正常的工具结果出现——
没有报错、没有告警，模型接着把内容复述给用户。

## 为什么每次都重新 realpath

不在入库时解析一次然后存下来。符号链接指向哪里是**会变的**：入库那一刻
``~/work/data`` 指向已授权目录，之后它可以被指到别处。每次校验都对根和目标两边
重新 ``realpath``，比较的才是此刻磁盘上的真实位置。

## 为什么用前缀比较而不是 commonpath

``os.path.commonpath`` 会把 ``/a/bc`` 和 ``/a/b`` 的公共前缀算成 ``/a``，看起来
安全。但反过来用它判断"在不在根下面"要写成 ``commonpath([root, target]) == root``，
而那个写法在 Windows 上遇到不同盘符会**抛异常**而不是返回 False——异常从工具
处理器里漏出去的表现是一次 500，不是一次"路径不合法"。

这里用的是 ``target == root or target.startswith(root + os.sep)``。
``+ os.sep`` 那一段是必须的：少了它，``/home/user/work-secrets`` 会被判成在
``/home/user/work`` 之下。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from models import WorkspaceRoot


class RootError(ValueError):
    """路径不在任何已授权目录之内，或授权本身有问题。

    消息可以直接回给模型：它需要知道"这条路不能走"以及"该走哪条"。
    """


def list_roots(db: Session, user_id: str) -> list[WorkspaceRoot]:
    return (
        db.query(WorkspaceRoot)
        .filter(WorkspaceRoot.user_id == user_id)
        .order_by(WorkspaceRoot.created_at.asc())
        .all()
    )


def has_roots(db: Session, user_id: str) -> bool:
    """这个用户有没有授权过任何目录。

    工具注册要看它：没有根的时候文件系统工具不注册，而不是注册一个每轮都回
    "你还没授权任何文件夹"的版本——后者每轮都会被试一次，白烧上下文。
    """
    return (
        db.query(WorkspaceRoot.id)
        .filter(WorkspaceRoot.user_id == user_id)
        .first()
        is not None
    )


def add_root(
    db: Session,
    user_id: str,
    raw_path: str,
    *,
    workspace_id: str | None = None,
    label: str | None = None,
) -> WorkspaceRoot:
    """登记一个授权目录。

    路径必须**此刻真的是一个目录**。授权一个不存在的路径没有意义，而且它会让
    "为什么模型说找不到文件"变成一个查不出来的问题——工具那侧只会说路径不合法。

    重复授权同一个目录是幂等的（返回已有那行），不是攒出第二行。撤销时只删一行
    的话，另一行还在，授权看起来撤销了实际没有。
    """
    cleaned = (raw_path or "").strip().strip('"')
    if not cleaned:
        raise RootError("路径为空。")
    if not os.path.isdir(cleaned):
        raise RootError(f"{cleaned} 不是一个存在的目录。")

    existing = (
        db.query(WorkspaceRoot)
        .filter(WorkspaceRoot.user_id == user_id, WorkspaceRoot.path == cleaned)
        .first()
    )
    if existing is not None:
        return existing

    root = WorkspaceRoot(
        id=str(uuid.uuid4()),
        user_id=user_id,
        workspace_id=workspace_id,
        path=cleaned,
        label=(label or os.path.basename(cleaned.rstrip("/\\")) or cleaned)[:120],
        created_at=datetime.now(),
    )
    db.add(root)
    db.commit()
    db.refresh(root)
    return root


def remove_root(db: Session, user_id: str, root_id: str) -> bool:
    """撤销一次授权。按 id 而不是按路径——路径可能含引号、大小写差异。

    过滤条件里必须带 ``user_id``：少了它，任何人拿一个 id 就能撤销别人的授权。
    """
    root = (
        db.query(WorkspaceRoot)
        .filter(WorkspaceRoot.id == root_id, WorkspaceRoot.user_id == user_id)
        .first()
    )
    if root is None:
        return False
    db.delete(root)
    db.commit()
    return True


def _real(path: str) -> str:
    """``realpath`` + 去掉尾部分隔符。

    尾部分隔符会让前缀比较出错：``realpath("/a/b/")`` 在部分平台上返回 ``/a/b/``，
    而 ``"/a/b/" + os.sep`` 就变成了 ``/a/b//``，没有任何真实路径以它开头。
    """
    resolved = os.path.realpath(path)
    if len(resolved) > 1:
        resolved = resolved.rstrip("/\\")
    return resolved


def resolve_within_roots(db: Session, user_id: str, raw: str) -> str:
    """把模型给的路径解析成一个**确定在授权目录之内**的绝对路径。

    这是文件系统工具唯一的安全边界，所以它宁可拒绝也不猜：

    - 相对路径要求恰好一个授权根。多个根时无从判断相对于哪一个，猜错的后果是
      在错的目录里写文件。此时要求模型给绝对路径，并把根列出来。
    - 解析在 ``realpath`` **之后**做，所以符号链接、``..``、``.`` 全部已经展开。
    - 不检查目标是否存在：``write_file`` 要写的文件本来就还不存在。存在性由
      各个工具自己按语义判断。

    抛 ``RootError``，消息直接可回给模型。
    """
    cleaned = (raw or "").strip().strip('"')
    if not cleaned:
        raise RootError("path 为空。")

    roots = list_roots(db, user_id)
    if not roots:
        raise RootError(
            "你还没有授权任何本机文件夹，文件操作无法进行。"
            "请让用户在设置里添加一个工作文件夹。"
        )

    real_roots = [(root, _real(root.path)) for root in roots]

    if not os.path.isabs(cleaned):
        if len(real_roots) > 1:
            listed = "、".join(root.path for root, _ in real_roots)
            raise RootError(
                f"path 必须是绝对路径：当前授权了多个文件夹（{listed}），"
                "相对路径无法判断是相对于哪一个。"
            )
        cleaned = os.path.join(real_roots[0][1], cleaned)

    target = _real(cleaned)
    for _root, real_root in real_roots:
        # ``+ os.sep`` 不能省：少了它 /home/u/work-secrets 会被判成在 /home/u/work
        # 之下——前缀匹配成功，而那是另一个目录。
        if target == real_root or target.startswith(real_root + os.sep):
            return target

    listed = "、".join(root.path for root, _ in real_roots)
    raise RootError(
        f"path 超出已授权的文件夹范围。当前可以访问：{listed}。"
        "如果需要访问别的目录，请让用户先在设置里授权。"
    )


def describe_roots(db: Session, user_id: str) -> list[dict[str, Any]]:
    """给接口与提示词用的授权目录清单。"""
    return [
        {
            "id": root.id,
            "path": root.path,
            "label": root.label or os.path.basename(root.path) or root.path,
        }
        for root in list_roots(db, user_id)
    ]


__all__ = [
    "RootError",
    "add_root",
    "describe_roots",
    "has_roots",
    "list_roots",
    "remove_root",
    "resolve_within_roots",
]
