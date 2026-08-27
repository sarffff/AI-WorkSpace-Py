"""守卫：文件类型清单只能有一份。

这个文件测的不是"白名单对不对"——那是 ``test_service_security`` 的活（它按名字
断言 svg/exe 之类进不来）。测的是**结构**：七处消费者是否仍然派生自
``services/file_types.py``，以及有没有人又就地列了一份。

## 为什么值得一个专门的文件

2026-08-24 清点时这份清单有**七处**独立副本，而它们已经互相矛盾，并且已经产生
了两个真实故障：

- ``.html``：三处前端都收，两处后端都不收。知识库上传**直接 400**；对话附件那条
  有"尽力而为"的兜底，所以只是静默退化成内联全文。
- ``.svg``：前端当图片收，后端出于安全故意排除（可内嵌 ``<script>``，而
  ``/uploads`` 是原样回吐的静态服务）。图片分支没有兜底路径，所以选中一个 .svg
  **必然**弹"图片上传失败"。

清点本身也说明了问题的性质：第一轮只找到五处，把 ``workspace_tools`` 里那份和
``attachment_router`` 里就地的 ``image_exts`` 算进来才是七处。**人工清点会漏**，
所以收敛成一处之后必须有机器来盯住它，否则第八份迟早长出来。

这是本仓库反复出现的那个形状：两处配置语义耦合、不一致时零报错。同类的另外两个
是提示词版本与工具面（``prompt-version-must-match-tool-surface``）、以及降级只写
日志不进报告。三次的教训一致：**修法是消掉重复，而消掉之后要有守卫。**

## 为什么用 ``is`` 而不是 ``==``

``==`` 只能证明"今天的值一样"。有人把 ``TEXT_EXTENSIONS = file_types.TEXT`` 改回
一个恰好相等的字面量时，``==`` 照样绿——而那正是要防的东西（副本一开始都是相等
的，它们是**后来**才漂移的）。``is`` 断言的是"同一个对象"，也就是真的没有第二份。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from services import file_types

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

# 已知扩展名的全集，扫描器用它判断"这个字符串集合像不像一份扩展名清单"。
# 含 DELIBERATELY_EXCLUDED：有人重新列一份并把 svg 加回去，也要被抓到。
_UNIVERSE = (
    file_types.TEXT
    | file_types.IMAGE
    | file_types.DOCUMENT
    | file_types.DELIBERATELY_EXCLUDED
)


# ========== 派生身份：七处消费者仍指向同一个对象 ==========


def test_knowledge_service_text_extensions_is_the_source():
    """``parse_document`` 用它分派文本解析分支。"""
    from services import knowledge_service

    assert knowledge_service.TEXT_EXTENSIONS is file_types.TEXT


def test_knowledge_router_allowlist_is_the_source():
    """知识库上传闸门。此前含 .html 的那处 400 就在这条路上。"""
    from routers import knowledge_router

    assert knowledge_router._KNOWLEDGE_ALLOWED_EXT is file_types.KNOWLEDGE


def test_attachment_router_allowlist_is_the_source():
    """对话附件上传闸门。"""
    from routers import attachment_router

    assert attachment_router.ALLOWED_EXT is file_types.ATTACHMENT


def test_workspace_tools_image_extensions_is_the_source():
    """第七处副本，清点第一轮漏掉的那个。

    ``read_attachment`` 用它决定"这是图片，给出那句需要视觉模型的解释"而不是
    去做文本解析。漏改它的症状不是报错，是回答变得莫名其妙——某种图片会拿到
    "解析失败"而不是那句解释。
    """
    from services import workspace_tools

    assert workspace_tools._IMAGE_EXTENSIONS is file_types.IMAGE


# ========== 集合代数：派生关系本身 ==========


def test_knowledge_is_text_plus_document():
    """知识库不收图片：没有 OCR 链路，收下只会得到一个 status=indexed
    但检索不到的空文档，那是最贵的一种静默失败。"""
    assert file_types.KNOWLEDGE == file_types.TEXT | file_types.DOCUMENT
    assert not (file_types.KNOWLEDGE & file_types.IMAGE)


def test_attachment_is_all_three_categories():
    assert file_types.ATTACHMENT == (
        file_types.TEXT | file_types.IMAGE | file_types.DOCUMENT
    )


def test_the_three_categories_are_disjoint():
    """类别必须互斥，否则 ``category_of`` 的返回值取决于函数里的判断顺序，
    而调用方按类别分派上传路径——一个扩展名同时是 text 和 image 时，
    它走哪条路就成了实现细节。"""
    assert not (file_types.TEXT & file_types.IMAGE)
    assert not (file_types.TEXT & file_types.DOCUMENT)
    assert not (file_types.IMAGE & file_types.DOCUMENT)


@pytest.mark.parametrize(
    "extension,expected",
    [
        ("md", "text"),
        ("py", "text"),
        ("png", "image"),
        ("webp", "image"),
        ("pdf", "document"),
        ("svg", None),
        ("html", None),
        ("exe", None),
        ("", None),
    ],
)
def test_category_of_matches_the_sets(extension, expected):
    assert file_types.category_of(extension) is expected


def test_category_of_normalizes_case_and_leading_dot():
    """真实输入两种形状都有：``rsplit(".")`` 出来的裸扩展名，以及 accept 串里
    带点的形式。大写来自 Windows 上的 ``.PDF``。"""
    assert file_types.category_of(".PDF") == "document"
    assert file_types.category_of("PNG") == "image"
    assert file_types.category_of(".Md") == "text"


def test_every_category_member_resolves_to_that_category():
    """反向全覆盖：不依赖上面那张手写的参数表。"""
    for extension in file_types.TEXT:
        assert file_types.category_of(extension) == "text"
    for extension in file_types.IMAGE:
        assert file_types.category_of(extension) == "image"
    for extension in file_types.DOCUMENT:
        assert file_types.category_of(extension) == "document"


# ========== 排除项：结构上进不来 ==========


def test_deliberately_excluded_are_in_no_category():
    """和 ``test_service_security`` 那条按名字断言的不重复：这里断言的是
    **结构性**保证——排除项不在任何基础类别里，所以它们不可能出现在任何
    派生集合里。那条测的是"白名单里没有它"，这条测的是"它没法进来"。
    """
    for extension in file_types.DELIBERATELY_EXCLUDED:
        assert file_types.category_of(extension) is None
        assert extension not in file_types.ATTACHMENT
        assert extension not in file_types.KNOWLEDGE


def test_svg_and_html_specifically_stay_out():
    """点名这两个：它们不是假想的风险，是 2026-08-24 之前真实在产生故障的两个。

    svg 是安全问题（可内嵌 script，/uploads 原样回吐），html 是检索质量问题
    （标签污染分块，稀释 embedding 也污染 BM25）。要支持 html 的正确做法是加一条
    剥标签的解析分支，而不是把它放回白名单——所以这条断言拦的是那个捷径。
    """
    assert "svg" not in file_types.ATTACHMENT
    assert "html" not in file_types.ATTACHMENT
    assert "html" not in file_types.KNOWLEDGE


# ========== accept 串：前端拿到的就是这些集合 ==========


@pytest.mark.parametrize("key", ["knowledgeAccept", "attachmentAccept"])
def test_accept_strings_round_trip_to_their_sets(key):
    """accept 串必须能还原回集合本身。

    前端不再自己维护清单、完全依赖这两个串，所以"串和集合不一致"会让文件选择器
    过滤出用户传不上去的东西——正是改动前 KnowledgePage 的那个 bug。
    """
    expected = {
        "knowledgeAccept": file_types.KNOWLEDGE,
        "attachmentAccept": file_types.ATTACHMENT,
    }[key]
    rendered = file_types.payload()[key]
    assert {part.lstrip(".") for part in rendered.split(",")} == set(expected)


def test_accept_strings_are_sorted_for_a_stable_payload():
    """顺序对文件选择器没影响，但一个每次请求都在抖的字段会让前端的缓存和
    响应体 diff 都变得没法看。"""
    parts = file_types.payload()["attachmentAccept"].split(",")
    assert parts == sorted(parts)


def test_every_accept_entry_has_a_leading_dot():
    """漏点的话浏览器会把它当 MIME 类型匹配，静默失效——过滤器看起来在工作，
    实际什么都不过滤。"""
    for key in ("knowledgeAccept", "attachmentAccept"):
        for part in file_types.payload()[key].split(","):
            assert part.startswith(".") and len(part) > 1


def test_payload_categories_match_the_sets():
    """`/settings` 发出去的三个类别数组就是这三个集合。"""
    body = file_types.payload()
    assert set(body["text"]) == set(file_types.TEXT)
    assert set(body["image"]) == set(file_types.IMAGE)
    assert set(body["document"]) == set(file_types.DOCUMENT)


def test_settings_endpoint_exposes_file_types():
    """接线断言：payload 进了 capabilities。

    少了这一条，上面所有断言都可能在"前端根本收不到"的情况下全绿——而那就是
    这个仓库里"记录了但没冒泡到消费者那一层"的第四次重演。
    """
    import inspect

    from routers import settings_router

    source = inspect.getsource(settings_router.get_settings)
    assert "fileTypes" in source
    assert "file_types.payload()" in source


# ========== 第八份副本：能派生的已派生，不能派生的用测试盯 ==========


def test_vision_mime_table_covers_exactly_the_image_category():
    """``vision._MIME_TYPES`` 的**键集**必须等于 IMAGE。

    这一处没有改成派生，因为 MIME 值推不出来（``jpg`` → ``image/jpeg``），
    所以它只能靠测试盯住。而它确实是一份实质副本：``vision.py:111`` 用
    ``not in _MIME_TYPES`` 当准入判据。

    失效方式是**静默**的：往 IMAGE 加一种格式却忘了加 MIME，那种图片会在
    第 111 行被安静跳过——用户抱怨"它没看见我的图"，而日志里什么都没有。
    """
    from services import vision

    assert set(vision._MIME_TYPES) == set(file_types.IMAGE)


def test_attachment_signature_table_covers_the_image_category():
    """magic bytes 校验表也必须覆盖 IMAGE。

    这张表回答的是另一个问题（内容和扩展名是否相符），所以不该合并进
    ``file_types``。但漏一种格式时 ``_validate_image_magic_bytes`` 会返回 False，
    那种图片的每一次上传都被拒成"疑似伪造文件类型"。这个失败**响亮**
    （400 带明确文案），不像上面那条是静默的，但同样是配了等于没配。
    """
    from routers.attachment_router import _IMAGE_SIGNATURES

    covered = {ext for exts in _IMAGE_SIGNATURES.values() for ext in exts}
    assert file_types.IMAGE <= covered


def test_document_category_has_a_signature_constant():
    """DOCUMENT 里每一种格式都要有一个签名常量，否则改扩展名就能绕过解析器。

    这条是给块 3 / 块 4 留的闸门：docx / xlsx 都是 ZIP 容器（``PK\\x03\\x04``），
    往 DOCUMENT 加它们时必须一并加签名校验，而这条断言会在忘记时立刻红。

    判据是模块里存在 ``_<EXT>_SIGNATURE`` 常量——按命名约定查而不是扫源码文本：
    后者会被注释里出现的扩展名蒙过去。约定本身也因此被钉住了，加格式的人照抄
    现有的 ``_PDF_SIGNATURE`` 就能过。
    """
    from routers import attachment_router

    for extension in file_types.DOCUMENT:
        name = f"_{extension.upper()}_SIGNATURE"
        assert hasattr(attachment_router, name), (
            f"DOCUMENT 里有 .{extension}，但 attachment_router 里没有 {name}；"
            "文档类必须做 magic bytes 校验，否则改扩展名即可绕过解析器"
        )


# ========== 扫描器：有没有人又就地列了一份 ==========


def _extension_literals_in(path: pathlib.Path) -> list[tuple[int, set[str]]]:
    """找出文件里"看起来是一份扩展名清单"的集合/列表/元组字面量。

    判据是同一个字面量里出现 >= 3 个已知扩展名。阈值 3 而不是 2：
    ``_IMAGE_SIGNATURES`` 里有 ``("jpg", "jpeg")`` 这样的合法二元组，它回答的是
    "这个签名对应哪些扩展名"，不是一份清单。

    只看集合/列表/元组，**不看 dict**：``vision._MIME_TYPES`` 是合法的
    ext→MIME 映射，它的键集由上面那条专门的测试盯着。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            continue
        literals = {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        hits = literals & _UNIVERSE
        if len(hits) >= 3:
            found.append((node.lineno, hits))
    return found


def test_no_backend_module_relists_extensions():
    """``services/`` 与 ``routers/`` 里不该再有第二份扩展名清单。

    扫的是这两个目录而不是全后端：``eval/`` 与 ``scripts/`` 是离线工具，它们造
    测试语料时列一批文件名是正当的，混进来只会让这条断言变成噪声。
    """
    offenders: list[str] = []
    for directory in ("services", "routers"):
        for path in sorted((BACKEND / directory).rglob("*.py")):
            if path.name == "file_types.py":
                continue  # 真相源本身
            for lineno, hits in _extension_literals_in(path):
                offenders.append(
                    f"{path.relative_to(REPO)}:{lineno} 就地列了 {sorted(hits)}"
                )
    assert not offenders, (
        "发现就地维护的扩展名清单，应当改成从 services/file_types.py 派生：\n  "
        + "\n  ".join(offenders)
    )


# ========== 前端：两处 accept 必须来自后端 ==========
# 只断言两个不含歧义的信号（没有硬编码 accept、确实引用了 fileTypes），
# 不做 TS 的字面量扫描：那需要一个 TS parser，而用正则找 `new Set([...])`
# 会在注释和无关字符串上误报。更宽的"有人又搭了一个 Set"由代码评审兜。


@pytest.mark.parametrize(
    "relative_path",
    [
        "front-end/src/pages/chat/ui/ChatPage.tsx",
        "front-end/src/pages/knowledge/ui/KnowledgePage.tsx",
    ],
)
def test_frontend_pages_do_not_hardcode_accept(relative_path):
    """``accept=".txt,.md,..."`` 这个形状就是改动前的两个 bug 所在。"""
    source = (REPO / relative_path).read_text(encoding="utf-8")
    hardcoded = re.findall(r'accept="\.[^"]*"', source)
    assert not hardcoded, f"{relative_path} 硬编码了 accept：{hardcoded}"
    assert "fileTypes" in source, f"{relative_path} 没有引用后端给的 fileTypes"
