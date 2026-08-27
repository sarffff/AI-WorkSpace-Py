"""文件类型的单一真相源：哪些扩展名能解析、能上传、能内联。

## 为什么需要这个模块

改动之前这份清单有**六处**独立副本，而它们已经互相矛盾（2026-08-24 清点）：

| 位置 | 内容 |
|---|---|
| `knowledge_service.TEXT_EXTENSIONS` | 20 项文本，无 html |
| `knowledge_router._KNOWLEDGE_ALLOWED_EXT` | 上面 + pdf |
| `attachment_router.ALLOWED_EXT` | 上面 + 5 种图片，注释写明排除 svg/html |
| `ChatPage.TEXT_EXTENSIONS` | 23 项，**含 html** |
| `ChatPage.accept` | 文本 + 图片（**含 svg**）+ pdf |
| `KnowledgePage.accept` | **缺** csv/log/sh/java/go/rs/c/cpp，**含 html** |

两个由此产生的真实故障：

- `.html`：三处前端都收，两处后端都不收。ChatPage 有"尽力而为"的兜底会退化成
  内联全文（那句注释已经承认了这处偏差），KnowledgePage 没有兜底，**直接 400**。
- `.svg`：前端 `IMAGE_EXTENSIONS` 收，后端故意排除（可内嵌脚本，
  `test_service_security.test_attachment_extension_whitelist_excludes_executables`
  钉着这条）。而图片分支没有兜底，所以选中 .svg **必然**报"图片上传失败"。

这是本仓库反复出现的那个形状的第三个实例：**两处配置语义耦合、不一致时零报错**。
所以修法不是"把六处改对"，而是把它变成一处——六处改对之后第七处照样会长出来。

## 前端为什么不各留一份

`GET /settings` 的 `capabilities.fileTypes` 把这里的结论发给前端。给的是
**按界面命名的成品**（`knowledgeAccept` / `attachmentAccept`）而不是让前端自己拼：
两个界面收的子集本来就不同（知识库不收图片），让前端拼就是把"哪个界面收哪个子集"
这个决定又复制一份，而那正是要消掉的东西。

## 对齐方向是前端向后端收

html 与 svg 从前端去掉，而不是给后端加：

- svg：**安全**。可内嵌 `<script>`，而附件会被静态服务原样吐回浏览器。已有测试钉住。
- html：**检索质量**。当纯文本收下会让分块里塞满标签，稀释 embedding 也污染 BM25。
  真要支持得先剥标签，那是解析器的活，不是白名单的活。

代价说清楚：ChatPage 此前能把 .html 内联进 prompt（走兜底路径），这个能力没了。
需要的话正确做法是加一条解析分支，而不是把它放回白名单。
"""
from __future__ import annotations

# ========== 三个基础类别 ==========
# 划分依据是**处理方式**而不是"像不像文档"：同一类里的扩展名走完全相同的代码路径。

# 能按纯文本解码后直接进分块的。代码文件也在内——它们对 RAG 的价值是让人能问
# "这个函数在哪调用"，而分块与检索不需要区分自然语言和源码。
TEXT: frozenset[str] = frozenset({
    "txt", "md", "js", "ts", "tsx", "jsx", "json", "xml", "yaml", "yml",
    "css", "csv", "log", "sh", "java", "go", "rs", "c", "cpp", "py",
})

# 前端用 <img> 渲染、后端做 magic bytes 校验的。**不含 svg**：它是 XML，
# 可内嵌 <script>，而 /uploads 是原样吐回浏览器的静态服务。
IMAGE: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

# 二进制文档：要专门的解析器，不能按文本解码。
#
# 往这里加格式时必须同时在 ``attachment_router`` 加一个 ``_<EXT>_SIGNATURE``
# magic bytes 常量，否则改扩展名就能绕过解析器。
# ``test_file_types_single_source.test_document_category_has_a_signature_constant``
# 会在忘记时立刻红——这条闸门就是为了这一刻加的。
#
DOCUMENT: frozenset[str] = frozenset({"pdf", "docx", "xlsx"})


# ========== 按界面派生 ==========
# 这三个集合是**后端三处白名单的唯一来源**。派生而不是各写一遍，是这个模块的全部意义。

# 知识库收的：能解析成文本进分块的。图片不在内——没有 OCR 链路，
# 收下只会得到一个 status=indexed 但检索不到的空文档，那是最贵的一种静默失败。
KNOWLEDGE: frozenset[str] = TEXT | DOCUMENT

# 对话附件收的：知识库那些 + 图片（视觉模型能读）。
ATTACHMENT: frozenset[str] = TEXT | IMAGE | DOCUMENT


# ========== 刻意排除 ==========
# 白名单本身就是执行机制（不在 ATTACHMENT 里的一律拒），这个集合**不是**第二道闸门，
# 它只记录"曾经有人想加、被否掉了"的那几个和理由。作用是让下一个人不用重新推一遍。
DELIBERATELY_EXCLUDED: frozenset[str] = frozenset({
    "svg",   # XML，可内嵌 <script>；/uploads 原样回吐
    "html",  # 标签污染分块；要支持得先剥标签
    "htm",
    "exe", "bat", "ps1", "vbs",  # 可执行
})


def accept_attribute(extensions: frozenset[str]) -> str:
    """渲染成 <input type="file"> 的 accept 值：``.md,.pdf,.txt``。

    排序固定，这样 `/settings` 的响应体是稳定的——顺序对文件选择器没有影响，
    但一个每次请求都在抖的字段会让前端的缓存与 diff 都变得没法看。
    """
    return ",".join(f".{ext}" for ext in sorted(extensions))


def category_of(extension: str) -> str | None:
    """返回 ``text`` / ``image`` / ``document``，都不是则 ``None``。

    前端按类别分派上传路径（内联 / <img> / 进知识库），而"按类别分派"这件事
    此前是靠前端各自维护的两个集合加一句 ``ext !== "pdf"`` 硬编码实现的。
    """
    normalized = extension.lower().lstrip(".")
    if normalized in TEXT:
        return "text"
    if normalized in IMAGE:
        return "image"
    if normalized in DOCUMENT:
        return "document"
    return None


def payload() -> dict[str, object]:
    """发给前端的那份。见模块文档"前端为什么不各留一份"。"""
    return {
        "text": sorted(TEXT),
        "image": sorted(IMAGE),
        "document": sorted(DOCUMENT),
        "knowledgeAccept": accept_attribute(KNOWLEDGE),
        "attachmentAccept": accept_attribute(ATTACHMENT),
    }
