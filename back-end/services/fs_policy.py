"""凭据文件保护：授权目录**之内**哪些文件不给模型看。

## 为什么沙箱不够

``fs_roots.resolve_within_roots`` 回答的是"这个路径在授权目录里吗"。它不回答
"这个文件该不该给模型看"——目录之内的东西它一律放行。

而用户在界面上点"添加工作文件夹"时，心里想的是「让它读我的项目」，不是
「让它读我的密钥」。一个再普通不过的项目根目录里就躺着 ``.env``；实测
``read_file`` 会把 ``DB_PASSWORD=hunter2`` 原样读进上下文，``search_files``
还能按关键词把它找出来。这些内容随下一次模型调用发给提供商，落在它们的日志里。

沙箱的注释里那句"不是为了安全（沙箱已经管了）"说的是 ``_SKIP_DIRS``，那是为了
搜索有用（跳过 ``node_modules``）。凭据这件事此前**没有任何东西在管**。

## 判据是文件名，不是内容

按内容判（"这段看起来像不是私钥"）要先把文件读出来——那正是要避免的动作。
文件名的约定性足够强：``.env``、``id_rsa``、``*.pem`` 这些名字在业界是稳定的。

代价是**假阴**：一个叫 ``notes.txt`` 的文件里贴着密码，这里拦不住。所以这不是
一道密不透风的墙，是一道**默认防线**——它把"顺手读到"变成"必须刻意绕过"。

## 为什么拦下来要说出来，而不是装作不存在

装作不存在（返回"文件不存在"）会让模型认为路径写错了，于是换个写法再试一次，
一轮变三轮。而且它会向用户复述一个错误结论："你的项目里没有 .env"。

说清"被策略挡了"之后，模型的正确反应是告诉用户这件事——那也正是用户该知道的。

## 拦的方向：读、搜、写、删全都拦

只拦读是不够的：``write_file`` 覆盖 ``.env`` 会让服务连不上数据库，
``delete_file`` 删掉 ``.ssh/id_rsa`` 是不可逆的本机损坏。这类文件模型既不该看，
也不该动。

``list_directory`` 例外——它**照常列出名字**，只在后面标一个「受保护」。
不列的话用户会遇到"我明明看到那个文件，助手说没有"，而目录里有 ``.env``
这件事本身不是秘密，里面的值才是。
"""
from __future__ import annotations

import os
from fnmatch import fnmatch

from config import settings

# 受保护的文件名模式。对 basename 做大小写无关的 fnmatch。
#
# 收录标准是**约定性足够强**：这个名字在业界基本只用来放凭据。像
# ``config.json`` / ``settings.py`` 这种"有时装着密钥"的名字一律不收——
# 假阳会让人把整个开关关掉，那比放过几个更糟。
_SECRET_FILES = (
    # dotenv 家族
    ".env",
    ".env.*",
    "*.env",
    # SSH 私钥。故意不写 ``id_rsa*``：``id_rsa.pub`` 是公钥，拦它没有意义
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    # 证书与密钥库
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.asc",
    # 各种工具的凭据文件
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".git-credentials",
    ".htpasswd",
    ".dockercfg",
    "credentials",
    "credentials.json",
    "kubeconfig",
    "*.kubeconfig",
    # 显式命名成 secret 的
    "secrets.yaml",
    "secrets.yml",
    "secrets.json",
    "*.secret",
    "*.secrets",
    # 云服务商的服务账号密钥
    "*service-account*.json",
)

# 受保护的目录名。路径里**任何一段**命中就算。这些目录整个都是凭据存储，
# 不需要逐个文件名去猜——``.ssh`` 里除了 ``known_hosts`` 就没有该给模型的东西。
_SECRET_DIRS = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".gcloud",
        ".docker",
        ".password-store",
    }
)


def enabled() -> bool:
    return settings.FS_PROTECT_SECRETS


def _extra_patterns() -> tuple[str, ...]:
    """``FS_SECRET_EXTRA_PATTERNS`` 里配的组织自有名字。

    只能**加**不能减：没有"例外名单"。例外名单里写一个 ``*.pem`` 就能把整条
    防线静默打开，而那件事在配置文件里看起来只是一行。要放开就整个关掉
    （``FS_PROTECT_SECRETS=false``），那是个明确的、看得见的决定。
    """
    raw = settings.FS_SECRET_EXTRA_PATTERNS or ""
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def is_protected(path: str) -> bool:
    """这个路径是不是受保护的凭据文件。

    同时看文件名与所在目录：``~/.ssh/config`` 的文件名不像凭据，但它在 ``.ssh``
    里。反过来 ``project/.env`` 的目录很普通，文件名是命中的那一个。
    """
    if not enabled():
        return False
    normalized = str(path or "").replace("\\", "/")
    if not normalized:
        return False

    # 目录判定：任何一段命中即算。用 basename 之外的所有段，
    # 这样一个**叫** .ssh 的文件不会被目录规则误伤（它会走文件名规则）
    parts = [part.lower() for part in normalized.split("/") if part]
    if any(part in _SECRET_DIRS for part in parts[:-1]):
        return True

    name = os.path.basename(normalized).lower()
    if not name:
        return False
    for pattern in _SECRET_FILES + _extra_patterns():
        if fnmatch(name, pattern):
            return True
    return False


def refusal(path_label: str, *, action: str) -> str:
    """拦下来时回灌给模型的说明。

    要让模型能把这件事**转述给用户**，所以话说得完整：拦了什么、为什么、
    以及用户想让它做该怎么办。只写"拒绝访问"的话模型只会换个路径再试。
    """
    return (
        f"{action}失败：{path_label} 被凭据保护策略挡下了。"
        "这类文件（.env、私钥、云服务商凭据等）里通常是密码与密钥，"
        "读出来会随对话发给模型提供商，改或删会破坏本机环境。"
        "请直接把这件事告诉用户；如果确实需要处理它，"
        "请用户自己打开文件，或在设置里关掉凭据保护（FS_PROTECT_SECRETS）。"
    )


__all__ = ["enabled", "is_protected", "refusal"]
