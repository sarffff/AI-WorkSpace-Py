"""文档分块。

朴素的定长字符切分有三个问题：把标题和正文切开、把代码块切断、块脱离原文后
无法自解释。这里做四件事：

1. 按 Markdown 结构切分（标题层级、代码围栏、段落），只在语义边界断开；
2. 用 token 预算而不是字符数控制块大小，中英混排时才不会失控；
3. 给每个块加上所属标题路径（contextual chunk），既提升 embedding 质量，
   也让引用显示得出"这段来自哪一节"；
4. 不做跨段重叠——重叠会污染检索结果且放大存储，改由检索阶段的邻域扩展
   （见 retriever 的 context expansion）补全被切断的上下文。

**两种策略。** ``structural``（默认）就是上面这套，零成本。``semantic`` 换一个
断点判据：把正文切成句子、算相邻句向量的余弦距离，在距离突变处断开——话题真正
转折的地方才断，而不是恰好写了个空行的地方。

语义分块需要向量，而这个模块是**纯同步、零依赖**的（``test_chunking.py`` 那批
用例不碰网络、不碰数据库）。所以分工是：这里只负责"切成句子"和"拿着距离数组
决定在哪断"，embedding 调用留在 ``knowledge_service.index_document`` 里。

为什么不做 late chunking：它需要 token 级 hidden states 再按块池化，而
``/embeddings`` 每条输入只返回一个池化后的向量，hosted API 拿不到 token 级输出。
"""
from __future__ import annotations

import math
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


# ========== 语义分块 ==========


@dataclass(slots=True)
class SentenceUnit:
    """待向量化的最小单位。``atomic`` 的单位（代码块）永不参与距离判断。"""

    text: str
    heading_path: str
    atomic: bool = False
    # 标题：它前面必须断开，而且它自己不该独立成块（应当和后面的正文一起）
    heading: bool = False


def sentences_for_embedding(text: str, filename: str = "") -> list[SentenceUnit]:
    """把文档切成句子级单位，供上层批量向量化。

    仍然走 ``_parse_blocks``，所以标题层级、代码围栏这些结构信息一个不丢——
    语义分块换掉的只是"在哪断"，不是"怎么理解这份文档"。
    """
    if not text or not text.strip():
        return []
    blocks = _parse_blocks(text, _looks_like_markdown(text, filename))
    units: list[SentenceUnit] = []
    for block in blocks:
        if block.kind == "heading":
            units.append(
                SentenceUnit(
                    text=block.text,
                    heading_path=block.heading_path,
                    atomic=True,
                    heading=True,
                )
            )
            continue
        if block.kind == "code":
            # 代码整块保留。按句切代码毫无意义，算它和相邻正文的"语义距离"
            # 更没有意义——那个距离必然很大，会在每个代码块前后都断一刀。
            units.append(
                SentenceUnit(text=block.text, heading_path=block.heading_path, atomic=True)
            )
            continue
        for sentence in _split_units(block):
            cleaned = sentence.strip()
            if cleaned:
                units.append(
                    SentenceUnit(text=cleaned, heading_path=block.heading_path)
                )
    return units


def adjacent_distances(vectors: list[list[float]]) -> list[float]:
    """相邻向量的余弦距离。``distances[i]`` 是 i 与 i+1 之间的距离。

    自己算而不是用 ``EmbeddingService.cosine_similarity``：这个模块要保持零依赖，
    ``test_chunking.py`` 才能不碰 openai 客户端就跑起来。
    """
    distances: list[float] = []
    for left, right in zip(vectors, vectors[1:]):
        distances.append(1.0 - _cosine(left, right))
    return distances


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def _percentile(values: list[float], percentile: float) -> float:
    """线性插值分位数。

    用分位数而不是绝对阈值：余弦距离的绝对值随 embedding 模型变，换个模型
    绝对阈值就得重调，而分位数是自适应的——"只在最跳的那 5% 处断开"这个意图
    跟模型无关。
    """
    if not values:
        return float("inf")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(100.0, percentile)) / 100.0 * (len(ordered) - 1)
    low = int(math.floor(rank))
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def split_semantic(
    units: list[SentenceUnit],
    distances: list[float],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    percentile: float = 95.0,
    counter: TokenCounter | None = None,
) -> list[Chunk]:
    """按语义断点组块。

    断开的条件（任一成立）：

    1. 到了标题 —— 标题永远开启新块，这条优先于距离判断；
    2. 章节变了 —— 和 ``structural`` 一样，同一个块内的内容一定来自同一节；
    3. 相邻距离超过阈值 —— 这是语义分块真正新增的那一条；
    4. 再加就超 ``max_tokens`` —— 兜底，否则一篇话题连贯的长文会变成一个巨块。

    第 4 条不能省：语义距离只说明"话题没转"，不说明"这段塞得进上下文"。
    """
    counter = counter or get_token_counter()
    if not units:
        return []
    max_tokens = max(1, max_tokens)
    # atomic 单位（标题、代码块）不参与阈值统计：它们与相邻正文的距离必然很大，
    # 混进分位数会把阈值整体抬高，于是正文里真正的话题转折反而断不开。
    eligible = [
        distance
        for index, distance in enumerate(distances)
        if index + 1 < len(units) and not units[index].atomic and not units[index + 1].atomic
    ]
    threshold = _percentile(eligible, percentile)

    chunks: list[Chunk] = []
    current: list[SentenceUnit] = []
    current_tokens = 0

    def flush() -> None:
        if not current:
            return
        heading_path = current[0].heading_path
        body = "\n\n".join(unit.text for unit in current).strip()
        current.clear()
        if not body:
            return
        chunks.append(
            Chunk(
                content=_contextual_prefix(heading_path, body),
                heading_path=heading_path,
                index=len(chunks),
            )
        )

    for index, unit in enumerate(units):
        unit_tokens = counter.count(unit.text)
        if current:
            section_changed = unit.heading_path != current[0].heading_path
            over_budget = current_tokens + unit_tokens > max_tokens
            # distances[index - 1] 是 unit 与它前一个单位之间的距离
            semantic_break = (
                index - 1 < len(distances)
                and not unit.atomic
                and not current[-1].atomic
                and distances[index - 1] > threshold
            )
            if unit.heading or section_changed or over_budget or semantic_break:
                flush()
                current_tokens = 0
        current.append(unit)
        current_tokens += unit_tokens

    flush()
    # 单个单位就超预算（超长代码块、无标点长文）时退回硬切，与 structural 同一套
    return _enforce_budget(chunks, counter, max_tokens)


def _enforce_budget(
    chunks: list[Chunk], counter: TokenCounter, max_tokens: int
) -> list[Chunk]:
    """把超预算的块按字符窗口硬切。重新编号，保证 index 连续。"""
    result: list[Chunk] = []
    for chunk in chunks:
        if counter.count(chunk.content) <= max_tokens:
            result.append(Chunk(chunk.content, chunk.heading_path, len(result)))
            continue
        for piece in _slice_by_chars(chunk.content, counter, max_tokens):
            cleaned = piece.strip()
            if cleaned:
                result.append(Chunk(cleaned, chunk.heading_path, len(result)))
    return result



