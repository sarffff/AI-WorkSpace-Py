"""本机文件系统工具：列目录、读文件、按内容搜索、写、改、删。

## 三层约束，缺一层这组工具就不该开

1. **沙箱**（``fs_roots.resolve_within_roots``）：路径必须落在用户授权过的目录
   之内。这是唯一的边界，没有第二道。
2. **护栏**（``guardrails``）：读回来的内容是外部内容，和知识库分块、网页、附件
   走同一套防线。仓库里一个含注入文本的文件，模型读一次就等于把它当成了资料。
3. **审批**（``approval``）：写、改、删在执行前停下来等人点一次。删除还额外要求
   确认令牌（用户原话里说过要删）。

## 为什么读文件要带 offset/limit

``TOOL_RESULT_MAX_CHARS`` 默认 4000，一次回答的总预算 ``TOOL_RESULT_TOTAL_CHARS``
默认 12000。一个几万字的源文件一次读全就吃掉整轮预算，后面几轮什么都做不了了。
所以 ``read_file`` 按行切片并把"还有多少行"如实告诉模型，让它自己决定要不要接着读。

## 为什么 edit_file 要求唯一匹配

``old_text`` 在文件里出现多处时直接拒绝，而不是改第一处。改第一处是个**看起来
成功**的错误：工具返回"已修改"，模型据此向用户复述，而实际改的可能不是用户要改
的那一处。要求模型多带几行上下文把匹配唯一化，代价是它多读一次文件，收益是
"改错地方"这个类别不存在。
"""
from __future__ import annotations

import difflib
import os
from typing import Any

from sqlalchemy.orm import Session

from config import settings
from services import file_types, fs_backup, fs_policy, fs_roots
from services.guardrails import guard, mask_markup
from services.tool_runtime import ToolDefinition

# 搜索与列目录时一律跳过的目录名。不是为了安全（沙箱已经管了），是为了有用：
# 一个 node_modules 能把 FS_SEARCH_MAX_MATCHES 在第一个目录里就耗尽，
# 而用户想搜的东西在第三个目录里。
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        "dist",
        "build",
        ".next",
        ".idea",
        ".vscode",
    }
)

# 按文本读不出东西的扩展名。二进制文件里的"匹配"没有意义，而解码它们又慢又会
# 产出乱码——那些乱码会原样进模型上下文。
#
# 从 ``file_types`` 派生，**不在这里列**：扩展名清单在这个仓库里曾经有六处互相
# 矛盾的副本，``test_file_types_single_source`` 就是为了不再长出第七处而存在的
# （我第一版确实就地列了一份，那条测试立刻抓住了）。
_BINARY_EXTENSIONS = file_types.BINARY_UNREADABLE


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _read_text(path: str) -> str:
    """按 utf-8 读，失败退 gb18030。

    和 ``workspace_tools._http_get_text`` 同一个策略：中文 Windows 环境下 gb18030
    编码的文本文件很常见，而一律按 utf-8 读的表现是"这个文件读不了"，
    模型会以为文件坏了。
    """
    with open(path, "rb") as handle:
        payload = handle.read()
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("gb18030", errors="replace")


def _rel(path: str, roots: list[dict[str, Any]]) -> str:
    """把绝对路径显示成相对于某个授权根的形式。

    给模型看完整的本机绝对路径没有坏处（它已经能读到），但相对路径更短，
    而路径会在每一次列目录、每一次搜索命中里重复出现——那些字符都记在
    工具结果预算上。
    """
    for root in roots:
        real_root = fs_roots._real(root["path"])
        if path == real_root:
            return root["label"]
        if path.startswith(real_root + os.sep):
            return f"{root['label']}/{path[len(real_root) + 1:].replace(os.sep, '/')}"
    return path


# ========== list_directory ==========


def _build_list_tool(db: Session, user_id: str) -> ToolDefinition:
    async def list_directory(arguments: dict[str, Any]) -> str:
        raw = arguments.get("path")
        # path 省略时列**授权根本身**，而不是报参数错误：模型第一次接触这个
        # 工作区时并不知道有哪些目录，"先列一下"是它最该做的第一步。
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            roots = fs_roots.describe_roots(db, user_id)
            if not roots:
                return (
                    "还没有授权任何本机文件夹。请让用户在设置里添加一个工作文件夹。"
                )
            if len(roots) > 1:
                lines = [f"已授权 {len(roots)} 个文件夹："]
                lines += [f"- {root['label']}（{root['path']}）" for root in roots]
                lines.append("请指定 path 再列出其中的内容。")
                return "\n".join(lines)
            raw = roots[0]["path"]

        if not isinstance(raw, str):
            return "列目录失败：path 必须是字符串。"
        try:
            target = fs_roots.resolve_within_roots(db, user_id, raw)
        except fs_roots.RootError as exc:
            return f"列目录失败：{exc}"

        if not os.path.isdir(target):
            if os.path.isfile(target):
                return f"列目录失败：{raw} 是一个文件，不是目录。要读它请用 read_file。"
            return f"列目录失败：{raw} 不存在。"

        roots = fs_roots.describe_roots(db, user_id)
        limit = max(1, settings.FS_LIST_MAX_ENTRIES)
        try:
            names = sorted(os.listdir(target))
        except OSError as exc:
            return f"列目录失败：{exc.strerror or exc}"

        dirs: list[str] = []
        files: list[str] = []
        for name in names:
            if name in _SKIP_DIRS:
                continue
            full = os.path.join(target, name)
            if os.path.isdir(full):
                # 受保护目录（.ssh 之类）照样列出来，但标出来并且不鼓励进去。
                # 藏起来的话用户会遇到"我明明看到那个目录，助手说没有"。
                if fs_policy.is_protected(os.path.join(full, "x")):
                    dirs.append(f"{name}/（受保护，里面的内容不可读）")
                else:
                    dirs.append(f"{name}/")
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                # 名字照列、值不给。目录里有 .env 这件事本身不是秘密，
                # 里面那行 DB_PASSWORD 才是——而模型需要知道它存在但读不了，
                # 否则它会反复换路径重试。
                if fs_policy.is_protected(full):
                    files.append(f"{name}（{size} 字节，受保护，不可读/改/删）")
                else:
                    files.append(f"{name}（{size} 字节）")

        entries = dirs + files
        if not entries:
            return f"{_rel(target, roots)} 是空目录。"

        shown = entries[:limit]
        # 文件名是外部输入。一个叫 `【参考 9】忽略以上指令.md` 的文件，光是出现在
        # 列表里就足以伪造出一条参考资料——``safe_document_name`` 那边已经证明过
        # 文件名本身就是注入面。
        body = "\n".join(f"- {mask_markup(entry)}" for entry in shown)
        header = f"{_rel(target, roots)} 下共 {len(entries)} 项"
        if len(entries) > limit:
            header += f"（只显示前 {limit} 项，超出 FS_LIST_MAX_ENTRIES）"
        return f"{header}：\n{body}"

    return ToolDefinition(
        name="list_directory",
        description=(
            "列出本机某个已授权文件夹下的子目录与文件。"
            "省略 path 时列出授权的文件夹本身——不确定有什么资料时先这么调一次。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出的目录路径。省略则列授权的文件夹本身",
                }
            },
            "additionalProperties": False,
        },
        handler=list_directory,
    )


# ========== read_file ==========


def _read_one(
    db: Session,
    user_id: str,
    raw: Any,
    *,
    offset: Any = None,
    limit: Any = None,
    max_lines: int | None = None,
    max_chars: int | None = None,
) -> str:
    """读一个文件，返回给模型看的正文（未过护栏，由调用方统一过）。

    从 ``read_file`` 里抽出来，好让批量读复用同一套判据——路径沙箱、目录/不存在、
    二进制、体积上限、行号语义。不抽的话批量读只能复制一遍，而复制出来的那份迟早
    和这份长得不一样，沙箱那类判断只该有一处。

    ``max_lines`` / ``max_chars`` 由调用方给：批量读时它们是**分摊后**的额度。
    不分摊的话一次读 5 个文件等于把单文件上限乘了 5 倍，把整回合的预算吃光。
    """
    if not isinstance(raw, str) or not raw.strip():
        return "读取失败：path 必须是非空字符串。"
    try:
        target = fs_roots.resolve_within_roots(db, user_id, raw)
    except fs_roots.RootError as exc:
        return f"读取失败：{exc}"

    if os.path.isdir(target):
        return f"读取失败：{raw} 是一个目录。要看它的内容请用 list_directory。"
    if not os.path.isfile(target):
        return f"读取失败：{raw} 不存在。"

    # 凭据保护。位置在沙箱**之后**：先确认这个路径本来就归他管，再判断内容敏感性。
    # 反过来的话，一个越界路径会先收到"被凭据策略挡下"——那句话确认了那个路径
    # 存在且是敏感文件，等于用错误消息回答了一个不该回答的问题。
    if fs_policy.is_protected(target):
        return fs_policy.refusal(raw, action="读取")

    extension = _extension(target)
    if extension in _BINARY_EXTENSIONS:
        return (
            f"读取失败：.{extension} 是二进制格式，没有可直接读取的文本。"
            "PDF、Word 这类文档请让用户上传到知识库，再用检索按需读取。"
        )

    try:
        size = os.path.getsize(target)
    except OSError as exc:
        return f"读取失败：{exc.strerror or exc}"
    if size > settings.FS_READ_MAX_BYTES:
        return (
            f"读取失败：文件 {size} 字节，超过 FS_READ_MAX_BYTES "
            f"({settings.FS_READ_MAX_BYTES})。"
            "可以用 search_files 在里面按内容定位，再用 offset 读那一段。"
        )

    try:
        text = _read_text(target)
    except OSError as exc:
        return f"读取失败：{exc.strerror or exc}"

    lines = text.splitlines()
    total = len(lines)
    # offset 是**从 1 开始的行号**，和编辑器、报错栈、grep 输出一致。
    # 从 0 开始的话模型会把 read_file 的 offset 和它在别处看到的行号混起来，
    # 而错一行的表现是"内容对不上"，不是报错。
    ceiling_lines = max(1, max_lines or settings.FS_READ_MAX_LINES)
    offset = offset if isinstance(offset, int) and offset > 0 else 1
    limit = limit if isinstance(limit, int) and limit > 0 else ceiling_lines
    limit = min(limit, ceiling_lines)

    if offset > total and total > 0:
        return f"读取失败：offset {offset} 超过文件总行数 {total}。"

    chunk = lines[offset - 1 : offset - 1 + limit]
    body = "\n".join(f"{offset + index}\t{line}" for index, line in enumerate(chunk))
    chars = max(1, max_chars or settings.FS_READ_MAX_CHARS)
    truncated_chars = len(body) > chars
    if truncated_chars:
        body = body[:chars]

    roots = fs_roots.describe_roots(db, user_id)
    header = (
        f"【{_rel(target, roots)}】第 {offset}-{offset + len(chunk) - 1} 行，共 {total} 行"
    )
    end_line = offset - 1 + len(chunk)
    if end_line < total:
        # 如实告诉它还剩多少、下一次该从哪开始。少了这句，模型要么以为读完了，
        # 要么用 offset+limit 自己算——而它算错的时候会漏掉中间几行且毫无察觉。
        header += f"（还有 {total - end_line} 行未读，接着读请用 offset={end_line + 1}）"
    if truncated_chars:
        header += f"（本段按 {chars} 字符截断）"
    return f"{header}\n{body}"


def _build_read_tool(db: Session, user_id: str) -> ToolDefinition:
    async def read_file(arguments: dict[str, Any]) -> str:
        raw_paths = arguments.get("paths")
        raw_path = arguments.get("path")

        # 两个都给就是自相矛盾的调用。挑一个执行会让模型以为另一个也生效了，
        # 而它下一轮会照着那个错误的前提往下走。
        if raw_paths is not None and raw_path is not None:
            return "读取失败：path 与 paths 只能给一个。读多个文件时只用 paths。"

        if raw_paths is None:
            single = _read_one(
                db,
                user_id,
                raw_path,
                offset=arguments.get("offset"),
                limit=arguments.get("limit"),
            )
            # 文件内容是外部内容，和知识库分块、网页、附件走同一套防线。
            # 这是新增的注入面且比现有的都直接：仓库里一个含注入文本的文件，
            # 模型读一次就等于把它当成了资料。
            shielded, _report = guard.shield(
                single, label="文件内容", kind="read_file"
            )
            return shielded

        if not isinstance(raw_paths, list) or not raw_paths:
            return "读取失败：paths 必须是非空数组。"

        limit_files = max(1, settings.FS_READ_MAX_FILES)
        if len(raw_paths) > limit_files:
            return (
                f"读取失败：一次最多读 {limit_files} 个文件，这次给了 {len(raw_paths)} 个。"
                "请分几次读，或先用 search_files 缩小范围。"
            )

        # offset/limit 在批量模式下没有意义：它们是"同一个文件的第几段"，
        # 而这里每个文件都从头读。收下但明确拒绝，比静默忽略好——静默忽略会让
        # 模型以为自己拿到的是指定的那一段。
        if arguments.get("offset") is not None or arguments.get("limit") is not None:
            return (
                "读取失败：offset/limit 只能配合单个 path 用。"
                "批量读时每个文件都从头读，要读某个文件的中间一段请单独调一次。"
            )

        count = len(raw_paths)
        # 额度按文件数分摊，但给每个文件留一个下限：分摊到几行的时候读回来的东西
        # 没有意义，那种情况下宁可让模型看到"被截断"也不要给它一堆碎片。
        per_lines = max(20, settings.FS_READ_MAX_LINES // count)
        per_chars = max(200, settings.FS_READ_MAX_CHARS // count)

        sections = [
            _read_one(
                db, user_id, item, max_lines=per_lines, max_chars=per_chars
            )
            for item in raw_paths
        ]
        # 单个文件失败不中断整批：另外几个的内容仍然有用，而"哪个失败了"
        # 就写在它自己那一段里。整批报错会让模型为一个拼错的路径重读全部。
        joined = f"共读取 {count} 个文件（每个最多 {per_lines} 行）：\n\n" + "\n\n".join(
            sections
        )
        shielded, _report = guard.shield(joined, label="文件内容", kind="read_file")
        return shielded

    return ToolDefinition(
        name="read_file",
        description=(
            "读取本机已授权文件夹下的文本文件，返回带行号的片段。"
            "要读多个文件时用 paths 一次给全部路径——一次调用读完比一个个读省很多轮。"
            "单个文件较长时只返回一段，并告诉你还剩多少行，接着读就把 offset 设成它给的值。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "单个文件路径。要读多个文件请用 paths",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "一次读多个文件的路径数组。已经知道要读哪几个时用这个，"
                        "不要一个个调"
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": "从第几行开始读，从 1 开始。省略即从头读。只配合 path",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读几行。省略即用配置上限。只配合 path",
                },
            },
            "additionalProperties": False,
        },
        handler=read_file,
    )


# ========== search_files ==========


def _build_search_tool(db: Session, user_id: str) -> ToolDefinition:
    async def search_files(arguments: dict[str, Any]) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return "搜索失败：query 必须是非空字符串。"
        needle = query.strip()

        raw = arguments.get("path")
        roots = fs_roots.describe_roots(db, user_id)
        if not roots:
            return "还没有授权任何本机文件夹。请让用户在设置里添加一个工作文件夹。"

        if isinstance(raw, str) and raw.strip():
            try:
                bases = [fs_roots.resolve_within_roots(db, user_id, raw)]
            except fs_roots.RootError as exc:
                return f"搜索失败：{exc}"
            if not os.path.isdir(bases[0]):
                return f"搜索失败：{raw} 不是一个目录。"
        else:
            bases = [fs_roots._real(root["path"]) for root in roots]

        # 纯子串匹配，不做正则。模型写的正则出错时的表现是"搜不到"而不是报错，
        # 而它会据此判断"文件里没有这个东西"——那是个错误结论，且无从发现。
        # 需要正则的场景（找函数定义之类）由模型自己换个更具体的子串来近似。
        lowered = needle.lower()
        max_matches = max(1, settings.FS_SEARCH_MAX_MATCHES)
        max_files = max(1, settings.FS_SEARCH_MAX_FILES)
        hits: list[str] = []
        scanned = 0
        truncated = False
        # 因为凭据保护跳过了几个文件。必须报出来:不报的话"没找到"会被模型
        # 当成"库里没有这件事",而实际是有一个文件没敢看——两个结论的处置不同。
        protected_skipped = 0

        for base in bases:
            for dirpath, dirnames, filenames in os.walk(base):
                # 原地改 dirnames 才能真的不进去。过滤在这一层做而不是在下面判，
                # 一个 node_modules 能把 max_files 在第一个目录里就耗尽。
                dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
                for filename in sorted(filenames):
                    if _extension(filename) in _BINARY_EXTENSIONS:
                        continue
                    if scanned >= max_files:
                        truncated = True
                        break
                    full = os.path.join(dirpath, filename)
                    # 凭据文件整个跳过。这条比 read_file 那道门更要紧:搜索是
                    # **按关键词**命中的,搜 "PASSWORD" 会直接把 .env 里那一行
                    # 连值一起摘出来——比整篇读出去更精准地泄露。
                    if fs_policy.is_protected(full):
                        protected_skipped += 1
                        continue
                    try:
                        if os.path.getsize(full) > settings.FS_READ_MAX_BYTES:
                            continue
                        text = _read_text(full)
                    except OSError:
                        continue
                    scanned += 1
                    for index, line in enumerate(text.splitlines(), start=1):
                        if lowered in line.lower():
                            snippet = line.strip()[: settings.FS_SEARCH_SNIPPET_CHARS]
                            hits.append(
                                f"- {_rel(full, roots)}:{index}\t{mask_markup(snippet)}"
                            )
                            if len(hits) >= max_matches:
                                truncated = True
                                break
                    if len(hits) >= max_matches:
                        break
                if truncated or len(hits) >= max_matches:
                    break
            if truncated or len(hits) >= max_matches:
                break

        # 两处都要带上这句。"没找到"尤其需要它:少了这句,模型会把
        # "有一个凭据文件没敢搜"讲成"你的项目里没有这个东西"。
        protected_note = (
            f"（另有 {protected_skipped} 个受凭据保护的文件没有搜索，"
            "它们可能包含密码或密钥）"
            if protected_skipped
            else ""
        )

        if not hits:
            return (
                f"在 {scanned} 个文件里没有找到包含 {needle!r} 的行。{protected_note}"
                "可以换一个更短或更常见的说法，或先用 list_directory 确认搜的是不是这个目录。"
            )

        header = f"找到 {len(hits)} 处包含 {needle!r} 的行（扫了 {scanned} 个文件）"
        if truncated:
            header += "，结果已截断"
        if protected_note:
            header += protected_note
        # 命中的行是文件内容，同样是外部内容
        shielded, _report = guard.shield(
            f"{header}：\n" + "\n".join(hits), label="搜索结果", kind="search_files"
        )
        return shielded

    return ToolDefinition(
        name="search_files",
        description=(
            "在本机已授权文件夹里按内容搜索，返回匹配的文件、行号与该行内容。"
            "用来定位「哪个文件里提到了某件事」。纯子串匹配，不支持正则。"
            "省略 path 则搜索全部已授权文件夹。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的文本，纯子串匹配、不区分大小写",
                },
                "path": {
                    "type": "string",
                    "description": "限定搜索的目录。省略则搜索全部已授权文件夹",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=search_files,
    )


# ========== write_file ==========


def _build_write_tool(db: Session, user_id: str) -> ToolDefinition:
    async def write_file(arguments: dict[str, Any]) -> str:
        raw = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(raw, str) or not raw.strip():
            return "写入失败：path 必须是非空字符串。"
        if not isinstance(content, str):
            return "写入失败：content 必须是字符串。"
        try:
            target = fs_roots.resolve_within_roots(db, user_id, raw)
        except fs_roots.RootError as exc:
            return f"写入失败：{exc}"

        if os.path.isdir(target):
            return f"写入失败：{raw} 是一个目录。"
        # 凭据文件不给写。覆盖 .env 的后果是本机服务连不上数据库,而模型看到的
        # 是"写入成功"——它不知道自己刚把环境弄坏了。
        if fs_policy.is_protected(target):
            return fs_policy.refusal(raw, action="写入")
        limit = max(1, settings.AGENT_WRITE_MAX_CHARS)
        if len(content) > limit:
            return f"写入失败：content 超过 {limit} 字符，请自行精简或分次写入。"

        existed = os.path.isfile(target)
        # 不自动建父目录。模型把路径拼错时（少一层、多一层、目录名拼错）的表现
        # 会变成"悄悄多出一个目录树"，而它看到的是"写入成功"。让它先 list_directory
        # 确认目录存在，比事后清理一堆空目录便宜。
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            return (
                f"写入失败：目录 {os.path.dirname(raw)} 不存在。"
                "请先用 list_directory 确认目标目录，不要依赖自动创建。"
            )

        # 覆盖之前留一份旧内容。审批闸门挡的是"模型偷偷写"，挡不住"用户点了同意
        # 之后后悔"——而卡片上只看得到 diff 的前 60 行。备份失败不让写失败
        # （见 fs_backup.save 的文档串），但超上限没备份要如实说出来。
        note = fs_backup.save(user_id, target, action="write") if existed else None

        try:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
        except OSError as exc:
            return f"写入失败：{exc.strerror or exc}"

        roots = fs_roots.describe_roots(db, user_id)
        verb = "已覆盖" if existed else "已新建"
        return f"{verb} {_rel(target, roots)}（{len(content)} 字符）。{note or ''}"

    return ToolDefinition(
        name="write_file",
        description=(
            "把内容写入本机已授权文件夹下的一个文件，文件已存在则**整个覆盖**。"
            "只改其中一段请用 edit_file。目标目录必须已经存在。"
            "只在用户明确要求写文件时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "完整的文件内容"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=write_file,
    )


# ========== edit_file ==========


def _build_edit_tool(db: Session, user_id: str) -> ToolDefinition:
    async def edit_file(arguments: dict[str, Any]) -> str:
        raw = arguments.get("path")
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(raw, str) or not raw.strip():
            return "修改失败：path 必须是非空字符串。"
        if not isinstance(old_text, str) or not old_text:
            return "修改失败：old_text 必须是非空字符串。"
        if not isinstance(new_text, str):
            return "修改失败：new_text 必须是字符串。"
        if old_text == new_text:
            return "修改失败：old_text 与 new_text 相同，没有要改的东西。"
        try:
            target = fs_roots.resolve_within_roots(db, user_id, raw)
        except fs_roots.RootError as exc:
            return f"修改失败：{exc}"

        if not os.path.isfile(target):
            return f"修改失败：{raw} 不存在。"
        # 改比写更需要这道门:edit_file 要先把整个文件读出来才能匹配 old_text,
        # 也就是说少了这一句,一次"改 .env 里的某一行"会把整个 .env 读进内存,
        # 而失败消息里可能带上文件内容的片段。
        if fs_policy.is_protected(target):
            return fs_policy.refusal(raw, action="修改")
        try:
            text = _read_text(target)
        except OSError as exc:
            return f"修改失败：{exc.strerror or exc}"

        count = text.count(old_text)
        if count == 0:
            return (
                "修改失败：文件里找不到 old_text。"
                "请先用 read_file 看一遍当前内容，old_text 必须和文件里的字符逐位一致"
                "（包括缩进和换行）。"
            )
        if count > 1:
            # 改第一处是个**看起来成功**的错误：工具返回"已修改"，模型据此向用户
            # 复述，而改的可能不是用户要改的那一处。要求唯一匹配的代价是模型多读
            # 一次文件，收益是"改错地方"这个类别不存在。
            return (
                f"修改失败：old_text 在文件里出现了 {count} 次，无法确定改哪一处。"
                "请多带几行上下文，让它只匹配你要改的那一处。"
            )

        note = fs_backup.save(user_id, target, action="edit")

        try:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(text.replace(old_text, new_text, 1))
        except OSError as exc:
            return f"修改失败：{exc.strerror or exc}"

        roots = fs_roots.describe_roots(db, user_id)
        return (
            f"已修改 {_rel(target, roots)}"
            f"（替换 1 处，{len(old_text)} → {len(new_text)} 字符）。{note or ''}"
        )

    return ToolDefinition(
        name="edit_file",
        description=(
            "把本机文件里的一段文字替换成另一段。old_text 必须和文件里的字符逐位一致，"
            "且**在整个文件里只出现一次**——出现多次会被拒绝，请多带几行上下文。"
            "只在用户明确要求改文件时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "old_text": {
                    "type": "string",
                    "description": "要被替换掉的原文，必须逐位一致且唯一",
                },
                "new_text": {"type": "string", "description": "替换成的新内容"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        handler=edit_file,
    )


# ========== delete_file ==========


def _build_delete_tool(
    db: Session, user_id: str, delete_granted: bool
) -> ToolDefinition:
    async def delete_file(arguments: dict[str, Any]) -> str:
        raw = arguments.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return "删除失败：path 必须是非空字符串。"
        try:
            target = fs_roots.resolve_within_roots(db, user_id, raw)
        except fs_roots.RootError as exc:
            return f"删除失败：{exc}"

        if os.path.isdir(target):
            # 不删目录。递归删除的爆炸半径和删一个文件完全不是一个量级，
            # 而模型把路径少写一层的代价是整个子树没了——没有撤销按钮。
            return (
                f"删除失败：{raw} 是一个目录。本工具只删单个文件，"
                "删除整个目录请让用户自己在文件管理器里操作。"
            )
        if not os.path.isfile(target):
            return f"删除失败：{raw} 不存在（可能已经被删掉了）。"
        # 删凭据文件是不可逆的本机损坏:删掉 ~/.ssh/id_rsa 之后所有 git remote
        # 都连不上,而备份留的是我们自己的目录、用户不一定找得到。
        #
        # 这道门在确认令牌**之前**:令牌问的是"用户要求过删除吗",而这里的答案是
        # "这个文件无论谁要求都不删"。顺序反了的话,用户说一句"删掉"就会让
        # 拒绝理由变成"需要明确要求",而他明明已经要求了。
        if fs_policy.is_protected(target):
            return fs_policy.refusal(raw, action="删除")

        # 确认令牌：和 delete_knowledge_document 同一道门。单靠 description 里
        # 写"只在用户明确要求时使用"拦不住——模型可能把它刚读到的文件内容或网页里
        # 夹带的指令当成用户意图，而破坏性操作只做提示词约束等于没约束。
        # 令牌由 chat_service 按**用户原话**判定，这里只消费。
        if not delete_granted:
            return (
                "删除失败：删除本机文件是不可逆操作，需要用户在对话里明确要求过删除"
                "才会执行。请先向用户确认要删哪个文件，确认后我再删。"
            )

        roots = fs_roots.describe_roots(db, user_id)
        shown = _rel(target, roots)
        # 删除之前留一份。这是三个写操作里最需要它的：覆盖写至少还有 diff 可看，
        # 删掉的文件在界面上什么都不剩。
        note = fs_backup.save(user_id, target, action="delete")

        try:
            os.remove(target)
        except OSError as exc:
            return f"删除失败：{exc.strerror or exc}"
        return f"已删除 {shown}。{note or ''}"

    return ToolDefinition(
        name="delete_file",
        description=(
            "删除本机已授权文件夹下的一个文件，不可恢复。只删单个文件，不删目录。"
            "只在用户明确要求删除时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要删除的文件路径"},
                "reason": {
                    "type": "string",
                    "description": "删除原因（可选，用于告知用户）",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=delete_file,
    )


# ========== 组装 ==========

# 写操作的工具名。approval 那侧要按名字判断是否需要审批，
# chat_service 要按名字决定给不给确认令牌——两处都不该各自抄一份字符串。
WRITE_TOOLS = ("write_file", "edit_file", "delete_file")


def enabled() -> bool:
    return settings.TOOL_FS_ENABLED


def build(
    db: Session, user_id: str, *, delete_granted: bool = False
) -> list[ToolDefinition]:
    """按开关与授权状态组装文件系统工具。

    **没有授权目录就一个都不注册。** 不是注册一批然后每个都回"你还没授权"——
    那样模型每轮都会试一次，白烧上下文还拿不到东西（同 ``workspace_tools.build``
    里那条理由）。这也让"授权了才有文件能力"在工具面上是可见的。

    读工具随主开关一起给。写、改、删各自还有独立开关：读文件的失败模式是拿到
    错的内容，写文件的失败模式是改坏用户的东西，两者不该由同一个开关控制。

    ``delete_granted`` 是确认令牌，由调用方按用户原话计算；缺省即拒。
    """
    if not enabled() or not fs_roots.has_roots(db, user_id):
        return []
    tools = [
        _build_list_tool(db, user_id),
        _build_read_tool(db, user_id),
        _build_search_tool(db, user_id),
    ]
    if settings.TOOL_FS_WRITE_ENABLED:
        tools.append(_build_write_tool(db, user_id))
        tools.append(_build_edit_tool(db, user_id))
    if settings.TOOL_FS_DELETE_ENABLED:
        tools.append(_build_delete_tool(db, user_id, delete_granted))
    return tools


def enabled_names() -> list[str]:
    """当前配置下会注册哪些文件系统工具。给启动日志与自检用。

    注意它**不看授权状态**：授权是 per-user 的，而启动日志是进程级的。
    这里回答的是"配置允许哪些"，不是"某个用户现在有哪些"。
    """
    if not enabled():
        return []
    names = ["list_directory", "read_file", "search_files"]
    if settings.TOOL_FS_WRITE_ENABLED:
        names += ["write_file", "edit_file"]
    if settings.TOOL_FS_DELETE_ENABLED:
        names.append("delete_file")
    return names


# 界面侧的浏览接口（``routers/fs_router.py`` 的 /fs/browse）要跳过同一批目录、
# 把路径显示成同一种相对形式。做成公开别名而不是让路由去拿 ``_SKIP_DIRS``：
# 那两样东西的定义只该有一处，否则界面里会出现 node_modules 而模型看不到它，
# 同一个目录两种视图，排查起来是纯浪费。
SKIP_DIRS = _SKIP_DIRS
relative_label = _rel

__all__ = [
    "SKIP_DIRS",
    "WRITE_TOOLS",
    "build",
    "enabled",
    "enabled_names",
    "preview_extra",
    "relative_label",
]


# ========== 审批预览：diff ==========

# diff 里最多带多少行。审批卡片是给人看一眼就下判断的地方，几百行 diff 塞进去
# 既拖慢 SSE 也没人会读完——真要逐行核对该去看文件本身。
_DIFF_MAX_LINES = 60


def preview_extra(
    db: Session, user_id: str, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """写文件类操作在审批卡片上的额外预览：一段 unified diff。

    **必须在服务端算。** 前端没有文件访问权，它手里只有模型提议的参数——
    "把 old_text 换成 new_text"这种参数看不出这次改动到底动了什么，而用户正要
    在那个界面上点"同意"。

    只对 ``write_file`` / ``edit_file`` 产出。``delete_file`` 不给 diff：整个文件
    都要没了，给一份"全是减号"的 diff 不比一句"将永久删除"更有信息量，而它会让
    卡片变得很长——长到用户开始滚动而不是阅读。

    算不出来时返回空字典而不是抛异常。这是**预览**：路径越界、文件读不了、编码
    坏了，这些情况下审批本身仍然该照常进行（真正的拦截在工具执行时），
    只是卡片上少一块 diff。让预览的失败去打断审批是本末倒置。
    """
    if tool_name not in ("write_file", "edit_file"):
        return {}
    raw = arguments.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        target = resolve_path_for_preview(db, user_id, raw)
    except fs_roots.RootError:
        return {}

    # 凭据文件不出 diff。这是第七个调用点,而且是最容易漏的一个:它不在
    # read_file 那条路上,但 diff **就是文件内容**——一份 .env 的 diff 会把
    # 每一行密码原样送进 SSE 事件、渲染在审批卡片上。
    #
    # 而且这次调用注定会被工具拒绝(上面那三道门),给它算 diff 本来就没有意义。
    if fs_policy.is_protected(target):
        return {"__protected": True}

    try:
        before = _read_text(target) if os.path.isfile(target) else ""
    except OSError:
        return {}

    if tool_name == "write_file":
        after = arguments.get("content")
        if not isinstance(after, str):
            return {}
        if not before:
            # 新建文件没有"改动"可比。行数比一份全是加号的 diff 更有用:
            # 用户要判断的是"这个文件该不该存在"，而不是逐行读一份新内容。
            return {"__new_file": True, "__lines": len(after.splitlines())}
    else:
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return {}
        if not before or before.count(old_text) != 1:
            # 匹配不唯一时工具本身会拒绝执行。这里也不给 diff——替换第一处算出来的
            # diff 会展示一个**不会发生**的改动，而用户会据此点同意。
            return {}
        after = before.replace(old_text, new_text, 1)

    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="修改前",
            tofile="修改后",
            lineterm="",
            n=2,
        )
    )
    if not diff:
        return {}
    truncated = len(diff) > _DIFF_MAX_LINES
    body = diff[:_DIFF_MAX_LINES]
    # diff 的内容一部分来自模型（新内容），一部分来自磁盘上的文件（旧内容）——
    # 两边都是不可信文本。审批卡片是可信度最高的展示位（用户正准备在这里点同意），
    # 所以和 build_preview 里的字符串走同一道 mask_markup。
    return {
        "__diff": [mask_markup(line) for line in body],
        "__diff_truncated": truncated,
    }


def resolve_path_for_preview(db: Session, user_id: str, raw: str) -> str:
    """预览用的路径解析。就是沙箱那一个，单独包一层只为让上面那行读起来清楚。"""
    return fs_roots.resolve_within_roots(db, user_id, raw)
