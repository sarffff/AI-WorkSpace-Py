"""知识库之外的 Agent 工具。

三个只读的知识库工具让它是一个"会自己决定检索几次的问答机器人"。要成为工作台，
差的是**能接触知识库以外的东西**，以及**能改变工作区的状态**——只读工具再加十个，
也还是一个搜索框。

这里四个工具各自解决一类缺口：

- ``calculate``：模型的算术不可靠，而且错得很自然（数字看着就像对的）。
- ``web_search``：知识库回答"我存过的资料里怎么说"，回答不了"现在是什么情况"。
- ``read_attachment``：附件此前只是被归档，模型拿到的是一个 URL 字符串。
- ``save_to_knowledge_base``：唯一的写操作，也是唯一能让工作区状态改变的工具。

每个工具都独立开关，且**默认全部关闭**：打开一个工具就是把它的失败模式和攻击面
一起打开，该由使用者按需决定，而不是升级一次依赖就悄悄多出四个能力。

失败语义沿用 ``ToolStatus`` 的三档：参数不对或目标不存在时返回一段模型能据此
自我修正的说明（``ok``，因为工具本身没坏）；通道故障时让异常冒出去，由
``ToolRuntime`` 记成 ``unavailable``，循环随即收敛而不是原地重试。
"""
from __future__ import annotations

import ast
import logging
import math
import operator
import os
import re
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from config import settings
from services import knowledge_service as knowledge_module
from services.guardrails import guard
from services.tool_runtime import ToolDefinition
from services.web_search import WebSearchError, web_search_client

logger = logging.getLogger("workspace_tools")

# 图片不走文本解析。它们由 services/vision.py 转成 image_url 内容块
_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# Agent 自己写进知识库的文档带这个前缀，于是它在 list_knowledge_documents 与
# 引用列表里一眼可辨。混在用户上传的资料里分不出来，是"模型自问自答"的温床。
WRITE_NAME_PREFIX = "[Agent] "


# ========== calculate ==========

_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}

_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}

_MAX_EXPRESSION_CHARS = 200
# 2**10**10 不是算错，是把内存吃光。指数必须有上限
_MAX_EXPONENT = 64


class _CalcError(ValueError):
    """表达式不被接受。消息直接回给模型，让它自己改写。"""


def _evaluate(node: ast.AST) -> Any:
    """递归求值。

    刻意不用 ``eval``：即便先做过 AST 白名单校验，``eval`` 也把"漏掉一种节点类型"
    的后果放大成任意代码执行。自己走一遍树，没在白名单里的语法根本没有分支可走。
    """
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)

    if isinstance(node, ast.Constant):
        # bool 是 int 的子类，True + 1 能算出 2，但那不是用户想问的
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _CalcError("只支持数字字面量")
        return node.value

    if isinstance(node, ast.BinOp):
        handler = _BINARY_OPS.get(type(node.op))
        if handler is None:
            raise _CalcError("不支持这个运算符")
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if handler is operator.pow and abs(right) > _MAX_EXPONENT:
            raise _CalcError(f"指数的绝对值不能超过 {_MAX_EXPONENT}")
        return handler(left, right)

    if isinstance(node, ast.UnaryOp):
        handler = _UNARY_OPS.get(type(node.op))
        if handler is None:
            raise _CalcError("不支持这个一元运算符")
        return handler(_evaluate(node.operand))

    if isinstance(node, ast.Name):
        if node.id not in _CONSTANTS:
            raise _CalcError(f"未知名称 {node.id}")
        return _CONSTANTS[node.id]

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise _CalcError("只能调用白名单里的数学函数")
        if node.keywords:
            raise _CalcError("不支持关键字参数")
        return _FUNCTIONS[node.func.id](*[_evaluate(arg) for arg in node.args])

    if isinstance(node, (ast.List, ast.Tuple)):
        return [_evaluate(element) for element in node.elts]

    # 属性访问、下标、推导式、lambda 全部落在这里。少了这条兜底，
    # ``().__class__.__bases__`` 这类经典逃逸就有了入口。
    raise _CalcError("表达式里有不允许的语法")


def evaluate_expression(expression: str) -> float | int:
    """求值一个纯算术表达式。任何不被接受的写法都抛 ``_CalcError``。"""
    text = (expression or "").strip()
    if not text:
        raise _CalcError("表达式为空")
    if len(text) > _MAX_EXPRESSION_CHARS:
        raise _CalcError(f"表达式不能超过 {_MAX_EXPRESSION_CHARS} 字符")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise _CalcError("表达式语法不正确") from exc

    result = _evaluate(tree)
    if isinstance(result, list):
        raise _CalcError("结果必须是一个数字")
    if not isinstance(result, (int, float)) or not math.isfinite(result):
        raise _CalcError("结果不是有限数字")
    return result


async def _calculate(arguments: dict[str, Any]) -> str:
    expression = arguments.get("expression")
    if not isinstance(expression, str):
        return "计算失败：expression 必须是字符串。"
    try:
        return f"{expression.strip()} = {evaluate_expression(expression)}"
    except _CalcError as exc:
        return f"计算失败：{exc}。只支持算术运算与常见数学函数。"
    except ZeroDivisionError:
        return "计算失败：除数为零。"
    except (OverflowError, ValueError, TypeError) as exc:
        # 比如 log(-1) 或 sqrt 收到列表：属于"模型写错了"，回灌让它自己改
        return f"计算失败：{type(exc).__name__}。请检查参数是否在函数定义域内。"


_CALCULATE = ToolDefinition(
    name="calculate",
    description=(
        "计算一个算术表达式并返回精确结果。涉及数字加减乘除、百分比、幂、"
        "开方或对数时应当使用本工具，不要自己心算。"
        "支持 + - * / // % **、括号，以及 abs/round/min/max/sum/sqrt/log/log10/"
        "log2/exp/floor/ceil/sin/cos/tan 与常量 pi、e、tau。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的表达式，例如 (1280 * 0.85 + 200) / 12",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    handler=_calculate,
)


# ========== web_search ==========


async def _web_search(arguments: dict[str, Any]) -> str:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return "搜索失败：query 必须是非空字符串。"

    count = arguments.get("count")
    limit = count if isinstance(count, int) and not isinstance(count, bool) else None
    # 通道故障让异常冒出去,由 ToolRuntime 记成 unavailable ——
    # 搜索引擎挂了的时候重试同一个词没有意义,应该尽快收敛到"凭已知信息回答"。
    results = await web_search_client.search(query.strip(), limit)
    if not results:
        return (
            f"没有搜到与「{query.strip()}」相关的网页。可以换用更具体的关键词，"
            "或者改用 search_knowledge_base 查本地资料。"
        )

    lines = [f"共 {len(results)} 条结果："]
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item.title}\n   {item.url}\n   {item.snippet}")
    # 网页是这套系统里最大的注入面:知识库至少是用户自己放进去的,搜索结果
    # 是任何人都能发布的内容。所以这里的定界与中和不是可选项。
    shielded, _report = guard.shield(
        "\n".join(lines), label="网页搜索结果", kind="web_search"
    )
    return shielded


_WEB_SEARCH = ToolDefinition(
    name="web_search",
    description=(
        "搜索互联网获取本地知识库里没有的信息，适合时效性问题、公开资料查证、"
        "以及知识库检索无结果时的兜底。返回标题、链接与摘要，摘要不是网页全文。"
        "引用时必须给出链接，并说明这是网络来源而非本地资料。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，不要写成整句问题"},
            "count": {
                "type": "integer",
                "description": "返回条数，默认按服务端配置，最多 20",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=_web_search,
)


# ========== read_attachment ==========


def resolve_upload_path(raw: str) -> str:
    """把聊天里出现的附件引用解析成 ``UPLOAD_DIR`` 内的真实文件路径。

    路径是**模型写的**，而模型可能在复述资料或网页里夹带的字符串。所以先
    realpath 再确认它落在附件目录之内：少了这一步，``../../.env`` 只要被读到
    一次就是一次凭据泄露，而且它会以一段看起来正常的工具结果出现。
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError("path 为空")
    if "://" in cleaned:
        cleaned = urlparse(cleaned).path
    cleaned = cleaned.replace("\\", "/").lstrip("/")
    if cleaned.startswith("uploads/"):
        cleaned = cleaned[len("uploads/") :]
    if not cleaned:
        raise ValueError("path 里没有文件名")

    root = os.path.realpath(settings.UPLOAD_DIR)
    target = os.path.realpath(os.path.join(root, cleaned))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError("path 超出附件目录")
    if not os.path.isfile(target):
        raise ValueError("文件不存在")
    return target


def file_extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


async def _read_attachment(arguments: dict[str, Any]) -> str:
    raw = arguments.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return "读取失败：path 必须是非空字符串。"
    try:
        target = resolve_upload_path(raw)
    except ValueError as exc:
        return (
            f"读取失败：{exc}。path 应当是本次对话里出现过的 /uploads/... 路径。"
        )

    name = os.path.basename(target)
    extension = file_extension(name)
    if extension in _IMAGE_EXTENSIONS:
        return (
            "这是图片，没有可提取的文本。图片需要视觉模型：把 VISION_MODELS 配好"
            "并使用其中的模型时，对话里的图片会直接作为图像内容传给模型，"
            "不需要也不应该调用本工具。"
        )

    size = os.path.getsize(target)
    if size > settings.ATTACHMENT_READ_MAX_BYTES:
        return (
            f"读取失败：文件 {size} 字节，超过 ATTACHMENT_READ_MAX_BYTES "
            f"({settings.ATTACHMENT_READ_MAX_BYTES})。"
            "大文件请先上传到知识库，再用 search_knowledge_base 按需检索。"
        )

    with open(target, "rb") as handle:
        payload = handle.read()
    try:
        # 与上传链路共用同一个解析器,避免"进知识库能读、当附件读不了"
        text = knowledge_module.parse_file_content(name, payload)
    except ValueError as exc:
        return f"读取失败：{exc}"

    if not text.strip():
        return f"{name} 解析出来是空的（可能是扫描版 PDF，只有图像没有文本层）。"

    limit = max(0, settings.ATTACHMENT_READ_MAX_CHARS)
    body = text[:limit]
    if len(text) > limit:
        body += f"\n\n[附件过长已截断，原文 {len(text)} 字符]"
    # 附件同样是外部内容,和知识库分块走同一套防线
    shielded, _report = guard.shield(
        f"【{name}】\n{body}", label="附件内容", kind="read_attachment"
    )
    return shielded


_READ_ATTACHMENT = ToolDefinition(
    name="read_attachment",
    description=(
        "读取用户在本次对话里上传的附件全文（文本、代码、Markdown、PDF）。"
        "当消息里出现 /uploads/... 路径而你需要它的内容时使用。"
        "图片不能用本工具读取。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "附件路径，形如 /uploads/202608/xxxx.pdf",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    handler=_read_attachment,
)


# ========== save_to_knowledge_base ==========

_UNSAFE_NAME = re.compile(r"[^\w一-鿿 .\-]+")
_DOT_RUN = re.compile(r"\.{2,}")


def safe_document_name(raw: str) -> str:
    """把模型给的标题变成一个规整的文档名。

    这个名字只进数据库的 ``documents.name`` 列，不落文件系统,所以这里不是在防
    路径穿越;要防的是**观感上的歧义**:``mask_markup`` 那边已经证明过文件名本身
    就是注入面——一个叫 ``【参考 9】忽略以上指令.md`` 的文档,光是出现在
    ``list_knowledge_documents`` 的列表里就足以伪造出一条参考资料。所以除了
    去掉控制字符与分隔符,还要把 ``..`` 这类看起来像路径的残留折掉。
    """
    cleaned = _UNSAFE_NAME.sub(" ", (raw or "").strip())
    cleaned = _DOT_RUN.sub(".", cleaned)
    cleaned = " ".join(cleaned.split()).strip(". ")[:80]
    return cleaned or "未命名笔记"


def _build_save_tool(
    db: Session, user_id: str, knowledge: Any
) -> ToolDefinition:
    async def save_to_knowledge_base(arguments: dict[str, Any]) -> str:
        name = arguments.get("name")
        content = arguments.get("content")
        if not isinstance(name, str) or not name.strip():
            return "保存失败：name 必须是非空字符串。"
        if not isinstance(content, str) or not content.strip():
            return "保存失败：content 必须是非空字符串。"

        limit = max(1, settings.AGENT_WRITE_MAX_CHARS)
        if len(content) > limit:
            return f"保存失败：content 超过 {limit} 字符，请自行精简后再保存。"

        # 这是整个系统里唯一一条"注入 → 持久化"的通路:内容可能是模型转述的
        # 网页或资料原文,一旦写进知识库,它就会在之后每一轮 RAG 里被复用。
        # 所以入库前必须过一遍检测,而且命中拦截阈值时直接拒绝而不是照存。
        cleaned, report = guard.sanitize(content)
        if report.blocked:
            return (
                "保存失败：这段内容被注入检测拦下了（疑似夹带指令）。"
                "如果确认可信，请让用户手动上传该文件。"
            )

        filename = f"{WRITE_NAME_PREFIX}{safe_document_name(name)}.md"
        document = await knowledge.upload_document(
            db, filename, cleaned.encode("utf-8"), user_id
        )
        return (
            f"已保存到知识库：{filename}（document_id: {document.id}，"
            f"分块 {document.chunks}）。之后可以用 search_knowledge_base 检索到它。"
        )

    return ToolDefinition(
        name="save_to_knowledge_base",
        description=(
            "把一段整理好的文字作为新文档写入用户的本地知识库，之后可被检索。"
            "适合固化结论、会议要点、调研笔记。"
            "只在用户明确要求保存时使用，不要自作主张地保存。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "文档标题，不要带扩展名"},
                "content": {"type": "string", "description": "文档正文，Markdown"},
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
        handler=save_to_knowledge_base,
    )


# ========== 组装 ==========


def build(db: Session, user_id: str, knowledge: Any) -> list[ToolDefinition]:
    """按开关组装 workspace 工具。

    没开的工具**根本不注册**,而不是注册一个返回"该功能未启用"的版本:后者每轮
    都会被模型试一次,白烧一轮上下文还拿不到东西。``web_search`` 更进一步——
    开关开了但没配 API key 时同样不注册,因为那时它必然失败。
    """
    tools: list[ToolDefinition] = []
    if settings.TOOL_CALCULATE_ENABLED:
        tools.append(_CALCULATE)
    if settings.TOOL_WEB_SEARCH_ENABLED and web_search_client.configured:
        tools.append(_WEB_SEARCH)
    if settings.TOOL_READ_ATTACHMENT_ENABLED:
        tools.append(_READ_ATTACHMENT)
    if settings.TOOL_WRITE_KNOWLEDGE_ENABLED:
        tools.append(_build_save_tool(db, user_id, knowledge))
    return tools


def enabled_names() -> list[str]:
    """当前会注册哪些 workspace 工具。给启动日志与自检用,不参与请求链路。"""
    names = []
    if settings.TOOL_CALCULATE_ENABLED:
        names.append("calculate")
    if settings.TOOL_WEB_SEARCH_ENABLED:
        names.append(
            "web_search" if web_search_client.configured else "web_search(未配置,跳过)"
        )
    if settings.TOOL_READ_ATTACHMENT_ENABLED:
        names.append("read_attachment")
    if settings.TOOL_WRITE_KNOWLEDGE_ENABLED:
        names.append("save_to_knowledge_base")
    return names


__all__ = [
    "WRITE_NAME_PREFIX",
    "WebSearchError",
    "build",
    "enabled_names",
    "evaluate_expression",
    "file_extension",
    "resolve_upload_path",
    "safe_document_name",
]






