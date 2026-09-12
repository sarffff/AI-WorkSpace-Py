"""凭据文件保护：授权目录**之内**哪些文件不给模型看。

沙箱的逃逸测试在 test_fs_roots.py（那边全是"能不能出去"）。这个文件测的是另一件
事：路径**没有**越界、就在用户自己授权的目录里，但它是 ``.env`` 或者私钥。

三组：

1. **判据本身**——哪些名字算凭据、哪些不算。假阳比假阴更值得测：
   ``id_rsa.pub`` 被拦住会让人把整个开关关掉，那时假阴就是全部。
2. **六个工具都拦**。漏一个就等于没做——模型会找到那个没拦的入口。
3. **拦下来之后说了什么**。"文件不存在"会让模型换个路径重试并向用户复述一个
   错误结论，所以拒绝必须自称是策略拦截。
"""
from __future__ import annotations

import pytest

from config import settings
from conftest import run
from services import fs_policy, fs_roots, fs_tools


@pytest.fixture(autouse=True)
def _fs_on(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_FS_WRITE_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_FS_DELETE_ENABLED", True)
    monkeypatch.setattr(settings, "FS_PROTECT_SECRETS", True)
    monkeypatch.setattr(settings, "FS_SECRET_EXTRA_PATTERNS", "")


@pytest.fixture
def root(tmp_path, db_real):
    work = tmp_path / "work"
    work.mkdir()
    (work / "readme.md").write_text("# 项目\n端口是 8080\n", encoding="utf-8")
    (work / ".env").write_text(
        "DB_PASSWORD=hunter2\nOPENAI_API_KEY=sk-live-abc123\n", encoding="utf-8"
    )
    (work / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    (work / "id_rsa.pub").write_text("ssh-rsa AAAAB3 someone@host\n", encoding="utf-8")
    ssh = work / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text("Host github.com\n  User git\n", encoding="utf-8")
    fs_roots.add_root(db_real, "u1", str(work))
    return work


def _tools(db, user_id="u1", **kwargs):
    return {tool.name: tool for tool in fs_tools.build(db, user_id, **kwargs)}


def _call(tool, **arguments):
    return run(tool.handler(arguments))


# ========== 判据 ==========


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        ".env.production",
        "prod.env",
        "id_rsa",
        "id_ed25519",
        "server.pem",
        "private.key",
        "cert.p12",
        "store.jks",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".pgpass",
        ".git-credentials",
        ".htpasswd",
        "credentials.json",
        "kubeconfig",
        "secrets.yaml",
        "db.secret",
        "gcp-service-account-key.json",
    ],
)
def test_这些名字算凭据(name):
    assert fs_policy.is_protected(f"/home/u/work/{name}") is True


@pytest.mark.parametrize(
    "name",
    [
        "readme.md",
        "config.json",
        "settings.py",
        "environment.md",
        # 公钥不是秘密。拦它是纯粹的假阳——而假阳会让人把开关整个关掉
        "id_rsa.pub",
        "id_ed25519.pub",
        "notes.txt",
        "package.json",
        "docker-compose.yml",
    ],
)
def test_这些名字不算凭据(name):
    assert fs_policy.is_protected(f"/home/u/work/{name}") is False


def test_受保护目录里的普通文件也算():
    """``.ssh/config`` 的文件名一点都不像凭据，但它在 ``.ssh`` 里。"""
    assert fs_policy.is_protected("/home/u/.ssh/config") is True
    assert fs_policy.is_protected("/home/u/.aws/config") is True
    assert fs_policy.is_protected(r"C:\Users\u\.kube\cache\x.json") is True


def test_目录规则只看路径中间段不看文件名():
    """一个**叫** ``.ssh`` 的文件走文件名规则，不该被目录规则误伤。

    钉这一条是因为实现里用了 ``parts[:-1]``——写成 ``parts`` 的话行为看起来
    一样（这个文件仍然被拦），但拦它的理由错了，而错的理由会在别处产出错的结果。
    """
    assert fs_policy.is_protected("/home/u/work/.ssh") is False


def test_开关关掉之后一律不拦(monkeypatch):
    monkeypatch.setattr(settings, "FS_PROTECT_SECRETS", False)
    assert fs_policy.is_protected("/home/u/work/.env") is False


def test_可以补组织自有的名字(monkeypatch):
    monkeypatch.setattr(settings, "FS_SECRET_EXTRA_PATTERNS", "*.corp-key, vault-*.json")
    assert fs_policy.is_protected("/home/u/work/prod.corp-key") is True
    assert fs_policy.is_protected("/home/u/work/vault-prod.json") is True
    # 补充名单不影响内置判据
    assert fs_policy.is_protected("/home/u/work/readme.md") is False


# ========== 六个工具 ==========


def test_读取凭据文件被拒(root, db_real):
    """最要紧的一条：读出来的内容会随下一次调用发给模型提供商。"""
    result = _call(_tools(db_real)["read_file"], path=str(root / ".env"))
    assert "hunter2" not in result
    assert "sk-live-abc123" not in result
    assert "凭据保护策略" in result


def test_批量读里的凭据文件被单独拒而不拖垮整批(root, db_real):
    """一次读三个文件，其中一个是 ``.env``。

    正常文件照常读到，凭据那个单独给拒绝说明。整批失败是错的：模型会重试整批，
    而重试同样会失败——它没有别的办法知道是哪一个出的问题。
    """
    result = _call(
        _tools(db_real)["read_file"],
        paths=[str(root / "readme.md"), str(root / ".env"), str(root / "id_rsa.pub")],
    )
    assert "端口是 8080" in result          # 普通文件读到了
    assert "ssh-rsa AAAAB3" in result       # 公钥不受影响
    assert "hunter2" not in result          # 凭据没漏
    assert "凭据保护策略" in result


def test_搜索跳过凭据文件并报出跳过了几个(root, db_real):
    """搜索比整篇读更精准地泄露：搜 PASSWORD 会把那一行连值一起摘出来。

    而"跳过了几个"必须报——不报的话"没找到"会被模型讲成"你项目里没有这个东西"。
    """
    result = _call(_tools(db_real)["search_files"], query="PASSWORD")
    assert "hunter2" not in result
    assert "受凭据保护" in result


def test_列目录照常列出名字但标记出来(root, db_real):
    """名字照列、值不给。

    藏起来会让用户遇到"我明明看到 .env，助手说没有"，而目录里有 .env 这件事
    本身不是秘密。模型也需要知道它存在但读不了，否则会反复换路径重试。
    """
    result = _call(_tools(db_real)["list_directory"], path=str(root))
    assert ".env" in result
    assert "受保护" in result
    assert "hunter2" not in result
    # 受保护目录同样标出来
    assert ".ssh/" in result


def test_写入凭据文件被拒(root, db_real):
    """覆盖 .env 之后本机服务连不上数据库，而模型看到的是"写入成功"。"""
    target = root / ".env"
    before = target.read_text(encoding="utf-8")
    result = _call(_tools(db_real)["write_file"], path=str(target), content="EMPTY=1\n")
    assert "凭据保护策略" in result
    assert target.read_text(encoding="utf-8") == before


def test_修改凭据文件被拒(root, db_real):
    """edit_file 要先把整个文件读出来才能匹配 old_text。"""
    target = root / ".env"
    result = _call(
        _tools(db_real)["edit_file"],
        path=str(target),
        old_text="hunter2",
        new_text="hunter3",
    )
    assert "凭据保护策略" in result
    assert "hunter2" in target.read_text(encoding="utf-8")


def test_删除凭据文件被拒_即使用户要求过(root, db_real):
    """确认令牌给了也不删。

    顺序要紧：策略这道门在令牌**之前**。反过来的话用户说一句"删掉"就会让拒绝
    理由变成"需要你明确要求"，而他明明已经要求了——那是一句会让人重复三次的话。
    """
    target = root / "id_rsa"
    result = _call(
        _tools(db_real, delete_granted=True)["delete_file"], path=str(target)
    )
    assert "凭据保护策略" in result
    assert target.exists()


def test_受保护目录下的文件同样六门全拦(root, db_real):
    """``.ssh/config`` 走的是目录规则，要确认它和文件名规则走同一条出口。"""
    target = root / ".ssh" / "config"
    tools = _tools(db_real, delete_granted=True)
    assert "凭据保护策略" in _call(tools["read_file"], path=str(target))
    assert "凭据保护策略" in _call(tools["delete_file"], path=str(target))
    assert target.exists()


# ========== 审批预览 ==========


def test_审批卡片不给凭据文件算diff(root, db_real):
    """diff **就是文件内容**。一份 .env 的 diff 会把每一行密码渲染在审批卡片上。

    这是第七个调用点，也是最容易漏的一个：它不在 read_file 那条路上。
    """
    extra = fs_tools.preview_extra(
        db_real,
        "u1",
        "write_file",
        {"path": str(root / ".env"), "content": "DB_PASSWORD=changed\n"},
    )
    assert extra == {"__protected": True}
    assert "hunter2" not in str(extra)


def test_普通文件照常算diff(root, db_real):
    """反向：保护不该顺手把正常的审批预览也关掉。"""
    extra = fs_tools.preview_extra(
        db_real,
        "u1",
        "write_file",
        {"path": str(root / "readme.md"), "content": "# 项目\n端口是 9090\n"},
    )
    assert extra
    assert "__protected" not in extra


# ========== 关掉之后 ==========


def test_关掉保护之后读得到(root, db_real, monkeypatch):
    """开关要真的是开关。

    留这条是因为"策略生效"和"策略永远生效"在正向测试里同形，而后者意味着
    用户没有任何办法处理自己的文件——那时他只能绕过整个产品。
    """
    monkeypatch.setattr(settings, "FS_PROTECT_SECRETS", False)
    result = _call(_tools(db_real)["read_file"], path=str(root / ".env"))
    assert "hunter2" in result
