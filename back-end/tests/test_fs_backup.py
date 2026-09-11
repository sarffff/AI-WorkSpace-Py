"""写操作的旧版本留存与恢复。

``write_file`` 是 ``open(path, "w")``——旧内容当场消失。审批闸门挡的是"模型偷偷写"，
它挡不住"用户点了同意然后后悔"，而审批卡片上只看得到 diff 的前 60 行。

这个文件要钉住的核心性质有三条，都不是"功能能用"那种：

1. **备份不在授权目录内。** 在里面的话 ``list_directory`` / ``search_files`` 会列出来、
   模型会把自己写坏的旧版本当成资料读、而 ``delete_file`` 能把回收站本身删掉。
2. **备份失败不让写操作失败。** 它是附加保障。反过来的话用户会看到"写入失败"
   而文件其实没动，比没有备份更难解释。
3. **判据是 user_id，撤销授权之后不能再恢复。** 后者等于绕过"用户已经收回权限"。
"""
from __future__ import annotations

import os

import pytest

from config import settings
from conftest import run
from services import fs_backup, fs_roots, fs_tools


@pytest.fixture(autouse=True)
def _fs_on(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_FS_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_FS_DELETE_ENABLED", True)


@pytest.fixture
def root(tmp_path, db_real):
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.md").write_text("原始内容\n第二行\n", encoding="utf-8")
    fs_roots.add_root(db_real, "u1", str(work))
    return work


def _tools(db, user_id="u1", **kwargs):
    return {tool.name: tool for tool in fs_tools.build(db, user_id, **kwargs)}


def _call(tool, **arguments):
    return run(tool.handler(arguments))


# ========== 覆盖写留旧版本 ==========


def test_覆盖写之后旧内容还能取回(root, db_real):
    tools = _tools(db_real)
    _call(tools["write_file"], path="notes.md", content="全新内容")

    rows = fs_backup.list_for_user(db_real, "u1")
    assert len(rows) == 1
    assert rows[0]["action"] == "write"

    fs_backup.restore(db_real, "u1", rows[0]["id"])
    assert (root / "notes.md").read_text(encoding="utf-8") == "原始内容\n第二行\n"


def test_新建文件不产生备份(root, db_real):
    """没有旧内容可留。产生一条空备份会让"可恢复"的列表里全是噪音。"""
    tools = _tools(db_real)
    _call(tools["write_file"], path="brand-new.md", content="x")
    assert fs_backup.list_for_user(db_real, "u1") == []


def test_改文件与删文件同样留旧版本(root, db_real):
    tools = _tools(db_real, delete_granted=True)
    _call(tools["edit_file"], path="notes.md", old_text="第二行", new_text="改过的第二行")
    _call(tools["delete_file"], path="notes.md")

    actions = [r["action"] for r in fs_backup.list_for_user(db_real, "u1")]
    # 倒序：删在前，改在后
    assert actions == ["delete", "edit"]


def test_删掉的文件能恢复回来(root, db_real):
    tools = _tools(db_real, delete_granted=True)
    _call(tools["delete_file"], path="notes.md")
    assert not (root / "notes.md").exists()

    rows = fs_backup.list_for_user(db_real, "u1")
    fs_backup.restore(db_real, "u1", rows[0]["id"])
    assert (root / "notes.md").read_text(encoding="utf-8") == "原始内容\n第二行\n"


# ========== 备份不在授权目录内 ==========


def test_备份不出现在列目录与搜索里(root, db_real, monkeypatch):
    """这是这个文件最要紧的一条。备份落在授权目录里的话，模型会读到自己写坏的
    旧版本、把它当成资料，而且它有 delete_file——能把回收站本身删掉。
    """
    tools = _tools(db_real)
    _call(tools["write_file"], path="notes.md", content="全新内容")

    listed = _call(tools["list_directory"], path=str(root))
    assert ".bak" not in listed and "fs_backups" not in listed

    found = _call(tools["search_files"], query="原始内容")
    # 旧内容只存在于备份里，搜索不该找到它
    assert "原始内容" not in found or "没有找到" in found


def test_备份目录不在任何授权根内(root, db_real):
    """沙箱应当拒绝备份目录的路径——它不该是模型可达的位置。"""
    with pytest.raises(fs_roots.RootError):
        fs_roots.resolve_within_roots(db_real, "u1", settings.FS_BACKUP_DIR)


# ========== 失败模式 ==========


def test_超过上限时不备份但如实说明(root, db_real, monkeypatch):
    """静默跳过会让用户以为有后路。"""
    monkeypatch.setattr(settings, "FS_BACKUP_MAX_BYTES", 10)
    tools = _tools(db_real)
    out = _call(tools["write_file"], path="notes.md", content="新")

    assert "超过备份上限" in out
    assert fs_backup.list_for_user(db_real, "u1") == []


def test_备份出错不让写操作失败(root, db_real, monkeypatch):
    """备份是附加保障。反过来的话用户看到"写入失败"而文件其实没动。"""

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fs_backup.shutil, "copy2", _boom)
    tools = _tools(db_real)
    out = _call(tools["write_file"], path="notes.md", content="全新内容")

    assert "已覆盖" in out
    assert (root / "notes.md").read_text(encoding="utf-8") == "全新内容"


def test_每个文件只留最近几份(root, db_real, monkeypatch):
    monkeypatch.setattr(settings, "FS_BACKUP_MAX_PER_FILE", 2)
    tools = _tools(db_real)
    for i in range(4):
        _call(tools["write_file"], path="notes.md", content=f"第 {i} 版")

    rows = fs_backup.list_for_user(db_real, "u1")
    assert len(rows) == 2
    # 超出的那几份连内容文件一起删掉了：留索引没文件会让恢复报"备份不存在"
    blobs = [
        f for f in os.listdir(settings.FS_BACKUP_DIR) if f.endswith(".bak")
    ]
    assert len(blobs) == 2


# ========== 越权 ==========


def test_别人的备份看不到也恢复不了(root, db_real, tmp_path):
    tools = _tools(db_real)
    _call(tools["write_file"], path="notes.md", content="全新内容")
    backup_id = fs_backup.list_for_user(db_real, "u1")[0]["id"]

    # bob 授权了自己的目录，但那份备份不是他的
    other = tmp_path / "bob"
    other.mkdir()
    fs_roots.add_root(db_real, "u2", str(other))

    assert fs_backup.list_for_user(db_real, "u2") == []
    with pytest.raises(fs_backup.BackupError):
        fs_backup.restore(db_real, "u2", backup_id)


def test_撤销授权之后旧备份不再可恢复(root, db_real):
    """否则等于绕过"用户已经收回了这个目录的权限"。"""
    tools = _tools(db_real)
    _call(tools["write_file"], path="notes.md", content="全新内容")
    backup_id = fs_backup.list_for_user(db_real, "u1")[0]["id"]

    row = fs_roots.list_roots(db_real, "u1")[0]
    fs_roots.remove_root(db_real, "u1", row.id)

    assert fs_backup.list_for_user(db_real, "u1") == []
    with pytest.raises(fs_backup.BackupError):
        fs_backup.restore(db_real, "u1", backup_id)


# ========== 恢复本身也可撤销 ==========


def test_恢复之前先把当前内容备份(root, db_real):
    """"点错了恢复"不该是又一次不可逆操作。撤销要能来回走。"""
    tools = _tools(db_real)
    _call(tools["write_file"], path="notes.md", content="第二版")
    first = fs_backup.list_for_user(db_real, "u1")[0]["id"]

    fs_backup.restore(db_real, "u1", first)
    assert (root / "notes.md").read_text(encoding="utf-8") == "原始内容\n第二行\n"

    # 恢复动作自己也留了一份，内容是被它覆盖掉的"第二版"
    rows = fs_backup.list_for_user(db_real, "u1")
    restore_row = next(r for r in rows if r["action"] == "restore")
    fs_backup.restore(db_real, "u1", restore_row["id"])
    assert (root / "notes.md").read_text(encoding="utf-8") == "第二版"


# ========== 授权目录不能包含回收站 ==========


def test_不能授权包含备份目录的目录(db_real, tmp_path, monkeypatch):
    """这是备份机制的边界漏洞：把备份目录的祖先授权成工作区，回收站就落进沙箱里，
    模型能读到自己写坏的旧版本，更糟的是能用 delete_file 把回收站本身删掉——
    于是"写操作可撤销"这个保证静默失效。

    判据是"包含"而不是"相等"：真正危险的是祖先目录。
    """
    workspace = tmp_path / "ws"
    (workspace / "nested" / "backups").mkdir(parents=True)
    monkeypatch.setattr(
        settings, "FS_BACKUP_DIR", str(workspace / "nested" / "backups")
    )

    with pytest.raises(fs_roots.RootError) as exc:
        fs_roots.add_root(db_real, "u1", str(workspace))
    assert "备份目录" in str(exc.value)


def test_备份目录的兄弟目录仍可授权(db_real, tmp_path, monkeypatch):
    """守卫不能过宽：同级的另一个目录和回收站没有包含关系，该放行。"""
    monkeypatch.setattr(settings, "FS_BACKUP_DIR", str(tmp_path / "backups"))
    (tmp_path / "backups").mkdir()
    sibling = tmp_path / "work"
    sibling.mkdir()

    assert fs_roots.add_root(db_real, "u1", str(sibling)) is not None
