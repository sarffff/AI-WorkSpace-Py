"""覆盖写、改文件、删文件之前留一份旧内容，以及把它放回去。

## 为什么需要它

``write_file`` 是 ``open(path, "w")``——旧内容当场消失，没有回收站。审批闸门挡住的
是"模型偷偷写"，它挡不住"用户点了同意然后后悔"，而后者是更常见的那一种：
审批卡片上只看得到 diff 的前 60 行，同意之后才发现覆盖掉的是别的东西。

## 备份放在授权目录**外面**

放在用户的工作目录里（比如 ``.ai-backup/``）有三个问题：``list_directory`` 和
``search_files`` 会把它列出来、模型会读到自己写坏的旧版本当成资料、而且模型有
``delete_file`` ——它能把回收站本身删掉。所以备份落在 ``FS_BACKUP_DIR``
（默认在后端目录下），路径不在任何授权根内，六个文件工具都碰不到。

## 一个文件一条链，按时间倒序

同一个文件被改三次就有三份备份，最新的排在最前面。``FS_BACKUP_MAX_PER_FILE``
控制每个文件留几份、``FS_BACKUP_MAX_BYTES`` 控制单份多大——超过上限的文件不备份，
而且**必须如实告诉调用方**（见 ``save`` 的返回值）：静默跳过会让用户以为有后路。

## 判据是 user_id

和 ``workspace_roots`` 一致：能不能恢复某个文件，取决于这个文件当初是不是这个人
授权的目录里的。备份索引里存 user_id，恢复时按它过滤——少了这一条，拿一个
backup_id 就能把别人机器上的文件覆盖掉。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from typing import Any

from sqlalchemy.orm import Session

from config import settings
from services import fs_roots
from services.clock import naive_now

logger = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """恢复失败。消息可以直接给用户看。"""


def _root() -> str:
    return os.path.realpath(settings.FS_BACKUP_DIR)


def _index_path() -> str:
    return os.path.join(_root(), "index.json")


def _load_index() -> list[dict[str, Any]]:
    path = _index_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        # 索引坏了不能让写操作失败：备份是附加保障，不是主流程。
        logger.warning("备份索引读不出来，按空索引继续", exc_info=True)
        return []
    return data if isinstance(data, list) else []


def _save_index(rows: list[dict[str, Any]]) -> None:
    os.makedirs(_root(), exist_ok=True)
    tmp = _index_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, _index_path())


def save(user_id: str, path: str, *, action: str) -> str | None:
    """把 ``path`` 现在的内容存一份。返回给模型/用户看的说明，没备份则返回 None。

    调用点在**真正写之前**。返回 None 有两种情况：文件本来不存在（新建，没有旧
    内容可留），或者超过 ``FS_BACKUP_MAX_BYTES``。第二种要让上层说出来。

    这个函数**不抛异常**：备份失败不该让写操作失败。那样用户会看到"写入失败"
    而文件其实没动，比没有备份更让人困惑。失败只记日志。
    """
    if not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > max(1, settings.FS_BACKUP_MAX_BYTES):
        return f"（{size} 字节，超过备份上限，这次没有留旧版本）"

    try:
        os.makedirs(_root(), exist_ok=True)
        backup_id = uuid.uuid4().hex
        blob = os.path.join(_root(), f"{backup_id}.bak")
        shutil.copy2(path, blob)
        rows = _load_index()
        rows.insert(
            0,
            {
                "id": backup_id,
                "user_id": user_id,
                "path": path,
                "action": action,
                "size": size,
                "created_at": naive_now().isoformat(timespec="seconds"),
            },
        )
        # 同一个文件只留最近几份。超出的连索引带文件一起删——留着索引却没有文件
        # 会让恢复报一个"备份不存在"的错，那比一开始就没有更难解释。
        keep = max(1, settings.FS_BACKUP_MAX_PER_FILE)
        seen: dict[tuple[str, str], int] = {}
        kept: list[dict[str, Any]] = []
        for row in rows:
            key = (row.get("user_id", ""), row.get("path", ""))
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= keep:
                kept.append(row)
            else:
                stale = os.path.join(_root(), f"{row.get('id')}.bak")
                try:
                    os.remove(stale)
                except OSError:
                    pass
        _save_index(kept)
        return None
    except OSError:
        logger.warning("备份失败，写操作继续", exc_info=True)
        return None


def list_for_user(db: Session, user_id: str) -> list[dict[str, Any]]:
    """这个人可恢复的备份。只列**当前仍在授权目录内**的那些。

    授权撤销之后旧备份不该还能恢复：那等于绕过"用户已经收回了这个目录的权限"。
    每一条都重新过一次沙箱，和写操作走同一个判据。
    """
    out: list[dict[str, Any]] = []
    for row in _load_index():
        if row.get("user_id") != user_id:
            continue
        path = str(row.get("path") or "")
        try:
            fs_roots.resolve_within_roots(db, user_id, path)
        except fs_roots.RootError:
            continue
        out.append(
            {
                "id": row.get("id"),
                "path": path,
                "action": row.get("action"),
                "size": row.get("size"),
                "createdAt": row.get("created_at"),
                "exists": os.path.isfile(path),
            }
        )
    return out


def restore(db: Session, user_id: str, backup_id: str) -> str:
    """把某份备份放回原位，返回被恢复的路径。

    恢复本身也是一次覆盖，所以**先把当前内容再备份一份**——否则"点错了恢复"
    又是一次不可逆操作。这一条让撤销可以来回走。
    """
    rows = _load_index()
    row = next(
        (
            item
            for item in rows
            if item.get("id") == backup_id and item.get("user_id") == user_id
        ),
        None,
    )
    if row is None:
        # 不区分"不存在"和"不属于你"：区分开会把别人有哪些备份漏出去
        raise BackupError("该备份不存在。")

    path = str(row.get("path") or "")
    try:
        target = fs_roots.resolve_within_roots(db, user_id, path)
    except fs_roots.RootError as exc:
        raise BackupError(f"无法恢复：{exc}") from exc

    blob = os.path.join(_root(), f"{backup_id}.bak")
    if not os.path.isfile(blob):
        raise BackupError("该备份的内容文件已经不在了。")

    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        raise BackupError("原来的目录已经不存在，无法恢复到那个位置。")

    save(user_id, target, action="restore")
    try:
        shutil.copy2(blob, target)
    except OSError as exc:
        raise BackupError(f"恢复失败：{exc.strerror or exc}") from exc
    return target


__all__ = ["BackupError", "list_for_user", "restore", "save"]
