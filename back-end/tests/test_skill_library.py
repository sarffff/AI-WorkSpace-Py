"""skill 库：读盘、frontmatter 校验、附带文件的路径边界。

校验为什么要在**加载时**抛而不是运行时兜：一份缺 description 的 skill 不会报错，
它只是永远不被选中——索引里那一行是模型做选择的唯一依据。所以这类错误的表现是
"我明明写了 SOP，agent 从来不用它"，而那没有任何日志指向它。
"""
from __future__ import annotations

import os

import pytest

from services import skill_library
from services.skill_library import SkillError


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    """把 SKILL_DIR 指到临时目录，并清掉缓存。"""
    monkeypatch.setattr(skill_library, "SKILL_DIR", str(tmp_path))
    monkeypatch.setattr(skill_library, "_builtin", None)
    return tmp_path


def _write_skill(root, folder: str, *, body: str = "照这个做。", **meta) -> None:
    """写一个 skill 目录。

    位置参数叫 ``folder`` 而不是 ``name``：``**meta`` 里要能传 ``name=...`` 来造
    "frontmatter 的 name 和目录名不一致"那个用例，同名会直接 TypeError。
    """
    directory = root / folder
    directory.mkdir(exist_ok=True)
    fields = {"name": folder, "description": f"{folder} 的用途"}
    fields.update(meta)
    lines = "\n".join(f"{key}: {value}" for key, value in fields.items())
    (directory / "SKILL.md").write_text(
        f"---\n{lines}\n---\n{body}", encoding="utf-8"
    )


# ========== 正常加载 ==========


def test_加载一个skill(skills_dir):
    _write_skill(skills_dir, "expense", body="第一步：核对额度。")
    skills = skill_library.reload()

    assert set(skills) == {"expense"}
    assert skills["expense"].description == "expense 的用途"
    assert skills["expense"].instructions == "第一步：核对额度。"
    assert skills["expense"].source == "builtin"
    assert skills["expense"].attachments == ()


def test_附带文件被登记(skills_dir):
    _write_skill(skills_dir, "report")
    (skills_dir / "report" / "模板.md").write_text("# 模板", encoding="utf-8")
    (skills_dir / "report" / "参考.txt").write_text("参考", encoding="utf-8")

    skills = skill_library.reload()
    assert skills["report"].attachments == ("参考.txt", "模板.md")


def test_二进制附带文件不登记(skills_dir):
    """附带文件是给模型读的模板与参考资料。二进制格式它读不了，
    收下只会让"为什么读不出来"变成一个要查的问题。
    """
    _write_skill(skills_dir, "report")
    (skills_dir / "report" / "logo.png").write_bytes(b"\x89PNG")
    (skills_dir / "report" / "模板.md").write_text("# 模板", encoding="utf-8")

    assert skill_library.reload()["report"].attachments == ("模板.md",)


def test_目录不存在时返回空(skills_dir, monkeypatch):
    """skill 是可选功能，没建目录不该让进程起不来。"""
    monkeypatch.setattr(skill_library, "SKILL_DIR", str(skills_dir / "nope"))
    monkeypatch.setattr(skill_library, "_builtin", None)
    assert skill_library.reload() == {}


def test_目录里的非目录文件被忽略(skills_dir):
    _write_skill(skills_dir, "expense")
    (skills_dir / "README.md").write_text("说明", encoding="utf-8")
    assert set(skill_library.reload()) == {"expense"}


# ========== 校验 ==========


def test_缺SKILL_md时抛错(skills_dir):
    (skills_dir / "broken").mkdir()
    with pytest.raises(SkillError, match="缺少 SKILL.md"):
        skill_library.reload()


def test_缺description时抛错(skills_dir):
    """description 是模型选 skill 的唯一依据。缺了它这份 skill 永远不被选中，
    而那不会报错——所以必须在加载时就拦住。
    """
    directory = skills_dir / "expense"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: expense\n---\n正文", encoding="utf-8"
    )
    with pytest.raises(SkillError, match="缺 description"):
        skill_library.reload()


def test_空description也算缺(skills_dir):
    _write_skill(skills_dir, "expense", description="")
    with pytest.raises(SkillError, match="缺 description"):
        skill_library.reload()


def test_正文为空时抛错(skills_dir):
    _write_skill(skills_dir, "expense", body="")
    with pytest.raises(SkillError, match="正文是空的"):
        skill_library.reload()


def test_没有frontmatter时抛错(skills_dir):
    directory = skills_dir / "expense"
    directory.mkdir()
    (directory / "SKILL.md").write_text("直接就是正文", encoding="utf-8")
    with pytest.raises(SkillError, match="缺少 frontmatter"):
        skill_library.reload()


def test_frontmatter没闭合时抛错(skills_dir):
    directory = skills_dir / "expense"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: expense\ndescription: x\n正文", encoding="utf-8"
    )
    with pytest.raises(SkillError, match="没有闭合"):
        skill_library.reload()


def test_name和目录名不一致时抛错(skills_dir):
    """load_skill 该按哪个查是个没有好答案的问题，而附带文件是按目录找的。"""
    _write_skill(skills_dir, "expense", name="报销审核")
    with pytest.raises(SkillError, match="必须和目录名一致"):
        skill_library.reload()


# ========== 附带文件的路径边界 ==========


def test_附带文件路径解析(skills_dir):
    _write_skill(skills_dir, "report")
    (skills_dir / "report" / "模板.md").write_text("# 模板", encoding="utf-8")
    skill_library.reload()

    path = skill_library.attachment_path("report", "模板.md")
    assert os.path.isfile(path)


def test_未登记的文件名被拒(skills_dir):
    """白名单式校验：文件名必须在该 skill 登记过的附带文件里。

    这一道就足以挡住 ``../../.env``——那个字符串不在任何 skill 的附带文件列表里。
    """
    _write_skill(skills_dir, "report")
    (skills_dir / "report" / "模板.md").write_text("# 模板", encoding="utf-8")
    skill_library.reload()

    with pytest.raises(SkillError, match="没有名为"):
        skill_library.attachment_path("report", "../../.env")


def test_越界路径被拒(skills_dir):
    _write_skill(skills_dir, "report")
    skill_library.reload()

    with pytest.raises(SkillError, match="没有名为"):
        skill_library.attachment_path("report", "../other/x.md")


def test_不存在的skill被拒(skills_dir):
    _write_skill(skills_dir, "report")
    skill_library.reload()

    with pytest.raises(SkillError, match="没有名为 'nope' 的内置 skill"):
        skill_library.attachment_path("nope", "x.md")


def test_可用附带文件会列在错误消息里(skills_dir):
    """模型下一步该做的是换一个文件名，而那需要知道有哪些。"""
    _write_skill(skills_dir, "report")
    (skills_dir / "report" / "模板.md").write_text("# 模板", encoding="utf-8")
    skill_library.reload()

    with pytest.raises(SkillError, match="模板.md"):
        skill_library.attachment_path("report", "不存在.md")


# ========== 仓库里那份真实的 skill ==========


def test_仓库自带的skill能通过校验():
    """不 mock SKILL_DIR，读真实目录。

    这一条钉的是"提交进仓库的 skill 必须是合法的"——``main.py`` 启动时会调
    ``validate()``，这里红了意味着服务起不来。
    """
    skills = skill_library.reload()
    assert "expense-review" in skills
    assert skills["expense-review"].attachments == ("报销额度标准.md",)
