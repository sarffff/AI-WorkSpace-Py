"""迁移链的静态约束。

这个文件测的是**不跑迁移也能查出来的错**。跑不了是因为 SQLite 上整条链从零执行
不通（``0001_baseline`` 只建一部分表，而 ``users`` 有指向 ``workspaces`` 的外键，
MySQL 建表时就检查、SQLite 不检查），而项目用的是 MySQL——那条路径由
``database.init_db`` 的 ``create_all`` + ``stamp`` 走，不是 ``upgrade from zero``。

所以这里钉的是几条静态性质。它们的共同点是：**在 SQLite 上完全没有症状，
在 MySQL 上是升级中途失败**——而"中途"意味着前几条已经跑完，库停在一个
半升级的状态上（MySQL DDL 非事务性，ALTER 成功了但版本号没写进去）。
"""
from __future__ import annotations

import os
import re

MIGRATIONS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations",
    "versions",
)

# alembic_version.version_num 的列宽。Alembic 建这张表时用的就是 VARCHAR(32)，
# 而它**不会**因为 revision id 变长而自动加宽。
_VERSION_NUM_WIDTH = 32


def _revisions() -> dict[str, dict[str, str | None]]:
    """解析出 {revision: {down_revision, file}}。

    正则而不是 import：这些模块顶层就 ``from alembic import op``，import 它们需要
    一个活的 alembic 上下文。而这里要检查的全是文本层面的性质。
    """
    found: dict[str, dict[str, str | None]] = {}
    for name in sorted(os.listdir(MIGRATIONS)):
        if not name.endswith(".py") or name.startswith("__"):
            continue
        src = open(os.path.join(MIGRATIONS, name), encoding="utf-8").read()
        rev = re.search(r'^revision: str = "([^"]+)"', src, re.M)
        down = re.search(
            r'^down_revision: Union\[str, None\] = (?:"([^"]+)"|None)', src, re.M
        )
        assert rev, f"{name}：没有解析到 revision"
        found[rev.group(1)] = {
            "down": down.group(1) if down and down.group(1) else None,
            "file": name,
        }
    return found


def test_revision_id不超过版本表的列宽():
    """超长的后果只在 MySQL 上出现，而且是升级**中途**失败。

    我这次就踩了：``0015_skill_version_and_required_inputs`` 是 38 字符，
    SQLite 上一路绿（那边不检查 VARCHAR 长度），MySQL 上是
    ``DataError 1406 Data too long for column 'version_num'``——而 ALTER TABLE
    已经执行完了（MySQL DDL 非事务性），于是库停在"列加了、版本号没记上"的状态，
    再跑一次是 ``Duplicate column name``。
    """
    too_long = {
        rev: len(rev)
        for rev in _revisions()
        if len(rev) > _VERSION_NUM_WIDTH
    }
    assert not too_long, (
        f"这些 revision id 超过 alembic_version.version_num 的 "
        f"{_VERSION_NUM_WIDTH} 字符上限：{too_long}"
    )


def test_文件名与revision_id一致():
    """不一致不会报错，只会让"这条迁移在哪个文件里"要靠 grep 找。

    改名的时候最容易漏这一步——revision 改了、文件名没改。
    """
    for rev, info in _revisions().items():
        assert info["file"] == f"{rev}.py", (
            f"{info['file']} 里的 revision 是 {rev!r}，文件名应当是 {rev}.py"
        )


def test_链是单条不分叉():
    """两条迁移声明同一个 down_revision 就是分叉，Alembic 会拒绝 upgrade head
    并要求指定目标——而那时报错信息说的是"多个 head"，不会告诉你是哪两条撞了。
    """
    revisions = _revisions()
    downs: dict[str, list[str]] = {}
    for rev, info in revisions.items():
        if info["down"]:
            downs.setdefault(info["down"], []).append(rev)
    forks = {down: revs for down, revs in downs.items() if len(revs) > 1}
    assert not forks, f"这些 revision 被多条迁移同时声明为上游：{forks}"

    heads = [rev for rev in revisions if rev not in downs]
    assert len(heads) == 1, f"应当只有一个 head，实际有：{heads}"

    # 从 head 往回走，必须能覆盖全部 revision（不漏、不断）
    chain: list[str] = []
    cursor: str | None = heads[0]
    while cursor:
        assert cursor in revisions, f"{cursor} 被引用为上游，但没有对应的迁移文件"
        chain.append(cursor)
        cursor = revisions[cursor]["down"]
    assert len(chain) == len(revisions), (
        f"链上有 {len(chain)} 条，目录里有 {len(revisions)} 条——有孤立的迁移"
    )


def test_每条迁移都有downgrade():
    """降级路径没人天天用，但它是回滚的唯一手段。

    缺了的表现是"升级出问题了、退不回去"，而那个时刻不是写 downgrade 的好时机。
    """
    for name in sorted(os.listdir(MIGRATIONS)):
        if not name.endswith(".py") or name.startswith("__"):
            continue
        src = open(os.path.join(MIGRATIONS, name), encoding="utf-8").read()
        assert "def downgrade()" in src, f"{name} 没有 downgrade"
