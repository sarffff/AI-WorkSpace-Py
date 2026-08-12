"""文档分块。

朴素的定长字符切分有三个问题：把标题和正文切开、把代码块切断、块脱离原文后
无法自解释。这里做四件事：

1. 按 Markdown 结构切分（标题层级、代码围栏、段落），只在语义边界断开；
2. 用 token 预算而不是字符数控制块大小，中英混排时才不会失控；
3. 给每个块加上所属标题路径（contextual chunk），既提升 embedding 质量，
   也让引用显示得出"这段来自哪一节"；
4. 不做跨段重叠——重叠会污染检索结果且放大存储，改由检索阶段的邻域扩展
   （见 retriever 的 context expansion）补全被切断的上下文。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from services.token_budget import TokenCounter, get_token_counter

DEFAULT_MAX_TOKENS = 320
DEFAULT_OVERLAP_TOKENS = 40

_FENCE_RE = re.compile(r"^\s*(?P<fence>```+|~~~+)")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*#*$")
# 句末标点：中文全角 + 英文句号/问号/感叹号后接空白
_SENTENCE_RE = re.compile(r"(?<=[。！？；!?;])|(?<=[.!?])(?=\s)")

_MARKDOWN_EXTENSIONS = {"md", "markdown", "mdx"}


@dataclass(slots=True)
class Chunk:
    """一个待入库的分块。``content`` 已包含标题路径前缀。"""

    content: str
    heading_path: str
    index: int


@dataclass(slots=True)
class _Block:
    kind: str  # heading | code | text
    text: str
    heading_path: str


def _looks_like_markdown(text: str, filename: str) -> bool:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension in _MARKDOWN_EXTENSIONS:
        return True
    return any(_HEADING_RE.match(line) for line in text.splitlines()[:200])


def _parse_blocks(text: str, markdown: bool) -> list[_Block]:
    """把文本解析成语义块：标题、代码围栏、段落。

    代码围栏整块保留（哪怕内部有空行或 # 开头的注释），否则代码会被段落
    切分和标题识别切碎。
    """
    blocks: list[_Block] = []
    heading_stack: list[tuple[int, str]] = []
    paragraph: list[str] = []
    fence: str | None = None
    code: list[str] = []

    def heading_path() -> str:
        return " > ".join(title for _level, title in heading_stack)

    def flush_paragraph() -> None:
        if paragraph:
            body = "\n".join(paragraph).strip()
            if body:
                blocks.append(_Block("text", body, heading_path()))
            paragraph.clear()

    for line in text.splitlines():
        if fence is not None:
            code.append(line)
            if line.strip().startswith(fence):
                blocks.append(_Block("code", "\n".join(code), heading_path()))
                code.clear()
                fence = None
            continue

        fence_match = _FENCE_RE.match(line)
        if fence_match:
            flush_paragraph()
            fence = fence_match.group("fence")
            code.append(line)
            continue

        heading_match = _HEADING_RE.match(line) if markdown else None
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group("hashes"))
            title = heading_match.group("title").strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            blocks.append(_Block("heading", line.strip(), heading_path()))
            continue

        if not line.strip():
            flush_paragraph()
            continue

        paragraph.append(line)

    if fence is not None and code:
        # 未闭合的围栏：按代码块收尾，避免整段被丢掉
        blocks.append(_Block("code", "\n".join(code), heading_path()))
    flush_paragraph()
    return blocks


def _split_units(block: _Block) -> list[str]:
    """把超长块拆成可重新打包的最小单位：代码按行，正文按句。"""
    if block.kind == "code":
        return block.text.splitlines() or [block.text]
    sentences = [part for part in _SENTENCE_RE.split(block.text) if part and part.strip()]
    return sentences or [block.text]


def _slice_by_chars(unit: str, counter: TokenCounter, max_tokens: int) -> list[str]:
    """单个单位仍超预算时按字符窗口硬切（超长单行、无标点长文）。"""
    tokens = max(1, counter.count(unit))
    chars_per_token = max(1, len(unit) // tokens)
    window = max(1, max_tokens * chars_per_token)
    return [unit[start : start + window] for start in range(0, len(unit), window)]


def _pack_units(
    units: list[str],
    counter: TokenCounter,
    max_tokens: int,
    overlap_tokens: int,
    joiner: str,
) -> list[str]:
    """贪心打包 + token 级重叠。只在硬切超长块时用到。"""
    packed: list[str] = []
    current: list[str] = []
    current_tokens = 0

    def take_overlap(chunk_units: list[str]) -> tuple[list[str], int]:
        """回收尾部 overlap_tokens 以内的单位，作为下一个窗口的前缀。"""
        carried: list[str] = []
        carried_tokens = 0
        for unit in reversed(chunk_units):
            unit_tokens = counter.count(unit)
            if carried_tokens + unit_tokens > overlap_tokens:
                break
            carried.insert(0, unit)
            carried_tokens += unit_tokens
        return carried, carried_tokens

    for unit in units:
        unit_tokens = counter.count(unit)
        if unit_tokens > max_tokens:
            if current:
                packed.append(joiner.join(current).strip())
                current, current_tokens = [], 0
            packed.extend(
                piece.strip() for piece in _slice_by_chars(unit, counter, max_tokens)
            )
            continue
        if current and current_tokens + unit_tokens > max_tokens:
            packed.append(joiner.join(current).strip())
            current, current_tokens = take_overlap(current)
        current.append(unit)
        current_tokens += unit_tokens

    if current:
        packed.append(joiner.join(current).strip())
    return [piece for piece in packed if piece]


def _contextual_prefix(heading_path: str, body: str) -> str:
    """给块加上标题路径。块自带标题时只补祖先路径，避免标题重复两遍。"""
    if not heading_path:
        return body
    if body.lstrip().startswith("#"):
        ancestors = heading_path.rsplit(" > ", 1)[0] if " > " in heading_path else ""
        return f"{ancestors}\n\n{body}" if ancestors else body
    return f"{heading_path}\n\n{body}"


def split_document(
    text: str,
    filename: str = "",
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    counter: TokenCounter | None = None,
) -> list[Chunk]:
    """把文档切成带标题上下文的分块。

    同一个块内的内容一定来自同一个章节：章节边界优先于填满 ``max_tokens``，
    这样检索命中的块不会横跨两个不相关的小节。
    """
    counter = counter or get_token_counter()
    if not text or not text.strip():
        return []
    max_tokens = max(1, max_tokens)
    # 重叠超过半个块会让相邻块高度重复，检索结果也会挤满同一段内容。
    overlap_tokens = max(0, min(overlap_tokens, max_tokens // 2))

    blocks = _parse_blocks(text, _looks_like_markdown(text, filename))
    chunks: list[Chunk] = []
    pending: list[_Block] = []

    def emit_pending() -> None:
        if not pending:
            return
        section = list(pending)
        pending.clear()
        heading_path = section[0].heading_path
        body = "\n\n".join(block.text for block in section).strip()
        if not body:
            return

        if counter.count(body) <= max_tokens:
            pieces = [body]
        else:
            # 外层已保证多块组合不超预算，走到这里只可能是单个超长块。
            block = section[0]
            joiner = "\n" if block.kind == "code" else ""
            pieces = _pack_units(
                _split_units(block), counter, max_tokens, overlap_tokens, joiner
            )

        for piece in pieces:
            chunks.append(
                Chunk(
                    content=_contextual_prefix(heading_path, piece),
                    heading_path=heading_path,
                    index=len(chunks),
                )
            )

    current_tokens = 0
    for block in blocks:
        block_tokens = counter.count(block.text)
        section_changed = bool(pending) and block.heading_path != pending[0].heading_path
        if section_changed or (pending and current_tokens + block_tokens > max_tokens):
            emit_pending()
            current_tokens = 0
        pending.append(block)
        current_tokens += block_tokens

    emit_pending()
    return chunks



