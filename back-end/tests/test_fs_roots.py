"""路径沙箱。文件系统工具的全部安全性压在 ``resolve_within_roots`` 上。

这个文件里的每一条都是**逃逸尝试**，不是参数校验。判据不是"报错信息好不好看"，
而是"这条路径有没有被放进授权目录之外"。

路径是模型写的，而模型可能在复述它刚读到的文件、刚抓的网页、或者知识库分块里
夹带的字符串。``read_attachment`` 那边已经踩过同一个形状：少了前缀校验，
``../../.env`` 被读到一次就是一次凭据泄露，且它以一段看起来正常的工具结果出现。
"""
from __future__ import annotations

import os

import pytest

from services import fs_roots


@pytest.fixture
def root(tmp_path, db_real):
    """一个已授权目录，里面有一个文件；外面也有一个文件（逃逸目标）。"""
    inside = tmp_path / "work"
    inside.mkdir()
    (inside / "notes.md").write_text("授权目录里的内容", encoding="utf-8")
    (tmp_path / "secret.env").write_text("API_KEY=leaked", encoding="utf-8")
    fs_roots.add_root(db_real, "u1", str(inside))
    return inside


# ========== 正常路径 ==========


def test_授权目录内的绝对路径可以解析(root, db_real):
    target = fs_roots.resolve_within_roots(db_real, "u1", str(root / "notes.md"))
    assert os.path.basename(target) == "notes.md"


def test_单个根时相对路径按那个根解析(root, db_real):
    target = fs_roots.resolve_within_roots(db_real, "u1", "notes.md")
    assert target == fs_roots._real(str(root / "notes.md"))


def test_根目录本身可以解析(root, db_real):
    """列目录要能列根本身，所以 target == root 不能被判成越界。"""
    assert fs_roots.resolve_within_roots(db_real, "u1", str(root)) == fs_roots._real(
        str(root)
    )


def test_还不存在的文件也能解析(root, db_real):
    """write_file 要写的文件本来就不存在。存在性由各个工具按语义自己判。"""
    target = fs_roots.resolve_within_roots(db_real, "u1", str(root / "new.md"))
    assert target.endswith("new.md")


# ========== 逃逸 ==========


def test_点点斜杠逃不出去(root, db_real):
    with pytest.raises(fs_roots.RootError, match="超出已授权"):
        fs_roots.resolve_within_roots(db_real, "u1", str(root / ".." / "secret.env"))


def test_多层点点逃不出去(root, db_real):
    with pytest.raises(fs_roots.RootError, match="超出已授权"):
        fs_roots.resolve_within_roots(
            db_real, "u1", str(root / ".." / ".." / ".." / "etc" / "passwd")
        )


def test_相对路径里的点点也逃不出去(root, db_real):
    """单根时相对路径会被拼到根上，拼完仍然要过前缀校验。"""
    with pytest.raises(fs_roots.RootError, match="超出已授权"):
        fs_roots.resolve_within_roots(db_real, "u1", "../secret.env")


def test_同前缀的兄弟目录不算在根之内(tmp_path, db_real):
    """``/home/u/work-secrets`` 不在 ``/home/u/work`` 之下。

    这一条钉的是 ``startswith(root + os.sep)`` 里那个 ``+ os.sep``。少了它，
    前缀匹配会成功——而匹配成功的是另一个目录。这是最容易在重构里被"简化"掉的
    一行，也是最难从表现上看出来的一个洞。
    """
    work = tmp_path / "work"
    work.mkdir()
    sibling = tmp_path / "work-secrets"
    sibling.mkdir()
    (sibling / "keys.txt").write_text("secret", encoding="utf-8")
    fs_roots.add_root(db_real, "u1", str(work))

    with pytest.raises(fs_roots.RootError, match="超出已授权"):
        fs_roots.resolve_within_roots(db_real, "u1", str(sibling / "keys.txt"))


@pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="平台不支持符号链接"
)
def test_指向外部的符号链接逃不出去(root, tmp_path, db_real):
    """符号链接是"每次都重新 realpath"这个设计的理由。

    入库时解析一次并存下来的做法挡不住这个：那一刻链接可能指向合法位置，
    之后它可以被指到别处。
    """
    link = root / "escape"
    try:
        os.symlink(str(tmp_path / "secret.env"), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接（Windows 需要开发者模式或管理员）")

    with pytest.raises(fs_roots.RootError, match="超出已授权"):
        fs_roots.resolve_within_roots(db_real, "u1", str(link))


def test_realpath指到根外面就拒绝(root, tmp_path, db_real, monkeypatch):
    """符号链接逃逸的**不依赖平台**版本。

    上面那条真建链接的测试在 Windows 上永远跳过（``os.symlink`` 需要开发者模式或
    管理员权限，WinError 1314）。而"最关键的逃逸向量只在开发机上被测过"等于没测——
    这个仓库的主要开发平台就是 Windows。

    符号链接对沙箱的唯一影响是让 ``realpath`` 返回一个根之外的路径，所以直接造出
    那个返回值。这条测的是判据本身（解析之后的前缀比较），而不是操作系统会不会
    如实展开链接。
    """
    outside = str(tmp_path / "secret.env")
    inside_link = str(root / "escape")
    真realpath = os.path.realpath

    def 假realpath(path, *args, **kwargs):
        # 只对那个"链接"撒谎，其余照常——否则连根自己都解析错了，
        # 测试会因为别的原因通过
        if os.path.normcase(str(path)) == os.path.normcase(inside_link):
            return 真realpath(outside)
        return 真realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", 假realpath)

    with pytest.raises(fs_roots.RootError, match="超出已授权"):
        fs_roots.resolve_within_roots(db_real, "u1", inside_link)


def test_没有授权任何目录时一律拒绝(db_real):
    """没有根就没有"之内"。这条消息要告诉模型去让用户授权，而不是让它换个路径重试。"""
    with pytest.raises(fs_roots.RootError, match="还没有授权"):
        fs_roots.resolve_within_roots(db_real, "u1", "/etc/passwd")


def test_空路径被拒(root, db_real):
    with pytest.raises(fs_roots.RootError, match="为空"):
        fs_roots.resolve_within_roots(db_real, "u1", "   ")


def test_多个根时相对路径被拒而不是猜一个(tmp_path, db_real):
    """猜错的后果是在错的目录里写文件，所以这里要求绝对路径并把根列出来。"""
    first = tmp_path / "a"
    first.mkdir()
    second = tmp_path / "b"
    second.mkdir()
    fs_roots.add_root(db_real, "u1", str(first))
    fs_roots.add_root(db_real, "u1", str(second))

    with pytest.raises(fs_roots.RootError, match="必须是绝对路径"):
        fs_roots.resolve_within_roots(db_real, "u1", "notes.md")


def test_多个根时每个根都能各自解析(tmp_path, db_real):
    first = tmp_path / "a"
    first.mkdir()
    second = tmp_path / "b"
    second.mkdir()
    fs_roots.add_root(db_real, "u1", str(first))
    fs_roots.add_root(db_real, "u1", str(second))

    assert fs_roots.resolve_within_roots(db_real, "u1", str(first / "x.md"))
    assert fs_roots.resolve_within_roots(db_real, "u1", str(second / "y.md"))


# ========== 授权是按用户隔离的 ==========


def test_别人的授权不给我用(root, db_real):
    """判据是 user_id。u2 没授权过任何目录，所以它连 u1 的根都进不去。

    这一条钉的是"授权是本机行为、按用户存"这个决定：同工作区的另一个人不该因为
    我授权了一个目录就能读它——他机器上可能根本没有这个路径。
    """
    with pytest.raises(fs_roots.RootError, match="还没有授权"):
        fs_roots.resolve_within_roots(db_real, "u2", str(root / "notes.md"))


def test_撤销授权要带user_id(root, db_real):
    """少了 user_id 过滤，任何人拿一个 id 就能撤销别人的授权。"""
    roots = fs_roots.list_roots(db_real, "u1")
    assert len(roots) == 1
    assert fs_roots.remove_root(db_real, "u2", roots[0].id) is False
    assert len(fs_roots.list_roots(db_real, "u1")) == 1
    assert fs_roots.remove_root(db_real, "u1", roots[0].id) is True
    assert fs_roots.list_roots(db_real, "u1") == []


# ========== 授权登记 ==========


def test_重复授权同一目录是幂等的(tmp_path, db_real):
    """攒出两行的话，撤销只删一行——授权看起来撤销了，实际还在。"""
    work = tmp_path / "work"
    work.mkdir()
    first = fs_roots.add_root(db_real, "u1", str(work))
    second = fs_roots.add_root(db_real, "u1", str(work))

    assert first.id == second.id
    assert len(fs_roots.list_roots(db_real, "u1")) == 1


def test_不存在的目录不能授权(tmp_path, db_real):
    """授权一个不存在的路径会让"为什么模型说找不到文件"变成查不出来的问题。"""
    with pytest.raises(fs_roots.RootError, match="不是一个存在的目录"):
        fs_roots.add_root(db_real, "u1", str(tmp_path / "nope"))


def test_文件路径不能当根授权(root, db_real):
    with pytest.raises(fs_roots.RootError, match="不是一个存在的目录"):
        fs_roots.add_root(db_real, "u1", str(root / "notes.md"))


def test_has_roots反映授权状态(tmp_path, db_real):
    """工具注册看这个：没有根就不注册，而不是注册一个每轮都失败的版本。"""
    assert fs_roots.has_roots(db_real, "u1") is False
    work = tmp_path / "work"
    work.mkdir()
    fs_roots.add_root(db_real, "u1", str(work))
    assert fs_roots.has_roots(db_real, "u1") is True
