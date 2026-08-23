"""摄取层清洗：解码、PDF 结构恢复、文本规整。

这个模块存在的理由不是"预处理让质量好一点"，而是一件具体得多的事：**脏输入会让
整条召回通道彻底失效，而且失效得完全没有声音**。三条都能在代码里对上：

1. GBK 文档按 utf-8 解码成一串 U+FFFD 之后，``retrieval_index.tokenize()`` 的两个
   正则（拉丁词元、CJK bigram）一个都匹配不到，BM25 建索引时 ``if not tokens:
   continue`` 直接跳过整块——**稀疏通道看不见这篇文档**。稠密通道拿到的也只是
   替换符的向量。而文档状态是 ``indexed``、``chunks=N``，界面上完全正常。
2. PDF 抽出来的文本没有 ``#`` 行，于是 ``chunking._looks_like_markdown`` 判 False，
   ``heading_path`` 恒为空：「给每块加标题路径」和「章节边界优先于填满
   ``max_tokens``」这两件事对 PDF 全都不生效。``chunking`` 模块文档里承诺的四件事
   只剩两件。
3. 抽取把一个词切成 ``RESOURCE_ EXHAUSTED``，bigram 与词元全变——而 BM25 通道
   存在的全部理由就是接住这类字面命中。

所以这里做的是把两条通道的输入契约补齐。

**为什么用 pdfplumber 而不是继续用 PyPDF2**：第 2 条只能靠字号和坐标修。
``PdfReader.extract_text()`` 返回的是一个字符串，里面既没有字号也没有位置，
拿它无论怎么正则都推不出标题层级。pdfplumber 的 ``extract_words`` 每个词带
``size`` / ``x0`` / ``top``，标题识别、页眉页脚定位、词内空格判定全依赖它们。
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from config import settings

logger = logging.getLogger("ingest_clean")

# ========== 文本规整 ==========

# 零宽字符与软连字符。它们在 tokenize() 眼里既不是拉丁词元也不是 CJK，
# 于是一个夹在词中间的 U+200B 就能让 `RESOURCE_EXHAUSTED` 变成两个词元。
_INVISIBLE = dict.fromkeys(
    [
        0x200B,  # 零宽空格
        0x200C,  # 零宽不连字
        0x200D,  # 零宽连字
        0x2060,  # word joiner
        0xFEFF,  # BOM 出现在正文中间时
        0x00AD,  # 软连字符
    ],
    None,
)
# 控制字符（保留 \t \n）。PDF 与某些导出工具会夹带 \x0c 分页符之类的东西
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_REPLACEMENT = "�"

# 全角 ASCII → 半角。只折这一段，不做整体 NFKC：NFKC 会顺手动到 CJK 兼容
# 字符（``㈱`` 之类）和一些标点，范围比这里需要的大得多。
# 折它的理由很实际：全角的 ４２９ 和半角的 429 在 tokenize() 里是**不同的词元**，
# 而金标准里 lexical 探针考的就是 429、P8 这类字面命中。
_FULLWIDTH_START = 0xFF01
_FULLWIDTH_END = 0xFF5E
_FULLWIDTH_OFFSET = 0xFF01 - 0x21
_IDEOGRAPHIC_SPACE = "　"


def _fold_fullwidth(text: str) -> str:
    if not any(_FULLWIDTH_START <= ord(char) <= _FULLWIDTH_END for char in text):
        return text.replace(_IDEOGRAPHIC_SPACE, " ")
    folded = []
    for char in text:
        code = ord(char)
        if _FULLWIDTH_START <= code <= _FULLWIDTH_END:
            folded.append(chr(code - _FULLWIDTH_OFFSET))
        elif char == _IDEOGRAPHIC_SPACE:
            folded.append(" ")
        else:
            folded.append(char)
    return "".join(folded)


def clean_text(text: str) -> str:
    """规整文本：换行、不可见字符、控制字符、全角 ASCII、行尾空白。

    只做**不改变语义**的规整。真正要判断"这段该不该留"的东西（页眉页脚）在
    ``extract_pdf`` 里做，因为那需要跨页信息，纯文本这一层看不到。
    """
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.translate(_INVISIBLE)
    normalized = _CONTROL_RE.sub("", normalized)
    normalized = _fold_fullwidth(normalized)
    normalized = _TRAILING_WS_RE.sub("", normalized)
    # 三个以上连续空行压成一个空行。_parse_blocks 遇到任何空行都会 flush 段落，
    # 所以这一步不改变分块结果，纯粹是别把空行当 token 存进库。
    normalized = _BLANK_RUN_RE.sub("\n\n", normalized)
    return normalized.strip()


def readable_ratio(text: str) -> float:
    """可读字符占比。入库自检用它判断"这份文档到底解出东西来了吗"。

    分母是非空白字符总数：一份正常文档这个数接近 1.0，一份被 ``errors="replace"``
    毁掉的 GBK 文档接近 0。返回 1.0 而不是 0.0 给空文本——空是另一种失败
    （``chunks == 0``），由调用方单独判，混在一起会让两种原因分不开。
    """
    meaningful = [char for char in text if not char.isspace()]
    if not meaningful:
        return 1.0
    bad = sum(1 for char in meaningful if char == _REPLACEMENT)
    return 1.0 - bad / len(meaningful)


# ========== 解码 ==========


@dataclass(frozen=True, slots=True)
class DecodeResult:
    text: str
    encoding: str
    replacement_ratio: float


def sniff_decode(content: bytes) -> DecodeResult:
    """嗅探编码并解码。顺序是 utf-8 严格解 → 配置的编码先验 → 嗅探器。

    **先严格试 utf-8**：utf-8 的多字节序列有自校验性，别的编码极难恰好通过，
    所以"能严格解通"基本等于"就是 utf-8"。反过来嗅探器对短文本会猜错，一份两行的
    utf-8 中文很容易被判成 GB18030，而后者也能解通——解出来是乱码但**不报错**。

    **然后才是配置的先验，最后才是嗅探器**，这一层是被实测逼出来的：一段 GBK
    编码的中文短句会被 charset-normalizer 判成 EUC-KR，解出来是一串谚文。这不是
    嗅探器的 bug——同一串字节在 GB18030 / EUC-KR / Shift-JIS 下**都能严格解通**，
    从字节本身无法分辨，任何检测器都不行。所以这里必须有一个先验，而先验属于配置。

    这个顺序的代价要说清楚：一份真正的韩文文档会被当成中文解错。对这个项目
    （中文语料、中文用户）这是正确的取舍，但它是取舍，不是正确性。

    最危险的一点：猜错编码时解出来的文本**没有任何替换符**，所以
    ``readable_ratio`` 抓不到它——那道自检只挡得住 ``errors="replace"`` 那一类。
    """
    if not content:
        return DecodeResult("", "utf-8", 0.0)

    try:
        # utf-8-sig 顺手吃掉 BOM。留着 BOM 的话它会变成正文第一个字符，
        # 而首行往往是 Markdown 标题，`﻿# 标题` 匹配不上 _HEADING_RE。
        return DecodeResult(content.decode("utf-8-sig"), "utf-8", 0.0)
    except UnicodeDecodeError:
        pass

    for encoding in _encoding_hints():
        try:
            return DecodeResult(content.decode(encoding), encoding, 0.0)
        except (UnicodeDecodeError, LookupError):
            continue

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(content).best()
    except Exception as exc:  # 嗅探器自身出问题不该让上传挂掉
        logger.warning("charset sniffing failed: %s", type(exc).__name__)
        best = None

    if best is not None:
        text = str(best)
        return DecodeResult(text, str(best.encoding or "unknown"), readable_ratio(text))

    # 兜底还是 replace，但这次调用方拿得到 ratio，能据此把文档判成 failed
    # 而不是标成 indexed 然后永远检索不到。
    text = content.decode("utf-8", errors="replace")
    return DecodeResult(text, "utf-8/replace", readable_ratio(text))


def _encoding_hints() -> list[str]:
    """按配置给出严格解码的候选顺序。

    默认只有 ``gb18030``：它是 GBK / GB2312 的超集，能解通那两者的一切字节，
    所以一条就够，列三条只是让同一件事有三个出口。
    """
    return [
        item.strip()
        for item in (settings.INGEST_ENCODING_HINTS or "").split(",")
        if item.strip()
    ]


# ========== PDF 结构恢复 ==========


@dataclass(slots=True)
class PdfExtraction:
    text: str
    pages: int = 0
    backend: str = "pdfplumber"
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Line:
    text: str
    size: float
    top: float
    right: float = 0.0  # 行末的 x 坐标，判断这行是不是被折行截断的
    full: bool = False  # 行末接近右边界 → 它是一段没写完的话
    level: int = 0  # 0 = 正文，1-6 = 标题层级


# 空格宽度大约是字号的四分之一。间隙比这个还小说明两段字符本来是连着的，
# 是按字距被切开的——那时补一个空格就等于凭空在词里插空格，而 BM25 的词元
# 正是按空格和字符类别切的。这个比例偏小是故意的：宁可漏补一个空格
# （`fooBar` 仍是一个词元），也不要在 `RESOURCE_EXHAUSTED` 中间插一个。
_SPACE_GAP_RATIO = 0.28
# 同一行的判定容差（点）。PDF 里同一行的基线会有亚像素抖动
_LINE_TOLERANCE = 2.5
# 字号超过正文这个倍数才算标题。1.15 是经验值：更小会把加粗的正文行误判成标题
_HEADING_SIZE_RATIO = 1.15
# 标题一般不长，也不以句末标点结尾。这两条是为了别把一整段大字号引言判成标题
_HEADING_MAX_CHARS = 80
_SENTENCE_END = "。！？；.!?;:："
# 行末 x 坐标达到本页最大行宽的这个比例，就认为这行是被折行截断的（见 _mark_full_lines）
_FULL_LINE_RATIO = 0.85
# 页眉页脚只在页面上下这个比例的区域里找
_MARGIN_ZONE = 0.10

_WORD_TAIL_RE = re.compile(r"[A-Za-z0-9_)\]}%.,;:!?'\"-]$")
_WORD_HEAD_RE = re.compile(r"^[A-Za-z0-9_(\[{'\"$#@]")
_DIGITS_RE = re.compile(r"\d+")
_LIST_HEAD_RE = re.compile(r"^\s*([-*+•]|\d+[.)、]|[（(]\d+[)）])\s")


def _needs_space(left: str, right: str) -> bool:
    """两段文本之间该不该加空格。

    只有拉丁/数字之间才需要。中文按字符切词时每个字都是一个 word，无条件用空格
    拼会把整篇中文变成「每 个 字 之 间 都 有 空 格」——而 ``tokenize()`` 的 CJK
    bigram 取的是**相邻字符**，插了空格 bigram 就全废了，稀疏通道对中文彻底失效。
    """
    if not left or not right:
        return False
    return bool(_WORD_TAIL_RE.search(left) and _WORD_HEAD_RE.match(right))


def _join_words(words: list[dict]) -> str:
    """把一行的词拼回文本，按几何间隙决定加不加空格。"""
    parts: list[str] = []
    previous: dict | None = None
    for word in words:
        text = str(word.get("text") or "")
        if not text:
            continue
        if previous is not None:
            gap = _as_float(word.get("x0")) - _as_float(previous.get("x1"))
            size = _as_float(word.get("size")) or _as_float(previous.get("size")) or 10.0
            if gap > size * _SPACE_GAP_RATIO and _needs_space(parts[-1], text):
                parts.append(" ")
        parts.append(text)
        previous = word
    return "".join(parts)


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _group_lines(words: list[dict]) -> list[_Line]:
    """按 ``top`` 把词分行。pdfplumber 给的是词，不是行。"""
    if not words:
        return []
    ordered = sorted(words, key=lambda word: (_as_float(word.get("top")), _as_float(word.get("x0"))))
    lines: list[_Line] = []
    bucket: list[dict] = []
    bucket_top = _as_float(ordered[0].get("top"))

    def flush() -> None:
        if not bucket:
            return
        text = _join_words(bucket).strip()
        if text:
            # 行字号取该行出现最多的那个，而不是最大的：一行里夹一个大号
            # 首字母或角标不该把整行判成标题
            sizes = Counter(round(_as_float(word.get("size")), 1) for word in bucket)
            lines.append(
                _Line(
                    text=text,
                    size=sizes.most_common(1)[0][0],
                    top=bucket_top,
                    right=max(_as_float(word.get("x1")) for word in bucket),
                )
            )
        bucket.clear()

    for word in ordered:
        top = _as_float(word.get("top"))
        if bucket and abs(top - bucket_top) > _LINE_TOLERANCE:
            flush()
            bucket_top = top
        bucket.append(word)
    flush()
    _mark_full_lines(lines)
    return lines


def _mark_full_lines(lines: list[_Line]) -> None:
    """标出"行末顶到右边界"的行。

    这是段落合并唯一可靠的信号。PDF 里没有段落标记，每一个折行都是一条独立的
    line，所以只靠"末尾没有句末标点"去合并会把**整页文字粘成一段**，顺带毁掉
    列表项和表格行。而排版规律是硬的：一段话被折行时前一行必然写到了右边界，
    一段话结束时最后一行通常留白。

    右边界取本页所有行 ``right`` 的最大值——PDF 里没有"页面正文宽度"这个元数据，
    只能从实际排版反推。
    """
    if not lines:
        return
    margin = max(line.right for line in lines)
    if margin <= 0:
        return
    for line in lines:
        line.full = line.right >= margin * _FULL_LINE_RATIO


# 数字之外还剩多少个"实字"才不算页码。页码是短模板 + 一个变化的数字：
# ``第 1 页`` 残余 2、``- 5 -`` 残余 0、``Page 12 of 30`` 残余 6。而位置在边缘
# 但仅数字不同的正文残余明显更长：``各页不同的边缘内容 1`` 是 9、
# ``表 1 显示了各部门的人员编制情况`` 是 14。取 6 两侧各留 2-3 字符余量。
_PAGE_NUMBER_RESIDUAL_MAX = 6

# 计算残余时要去掉的装饰字符。页码常写成 ``- 5 -``、``第 3 页 / 共 10 页``，
# 这些标记不承载语义，算进残余会把短页码误判成正文。
_DECORATION_RE = re.compile(r"[\s\-—–.,:：、（）()\[\]/|#]")


def _digit_residual(text: str) -> int:
    """去掉数字与装饰字符后还剩几个实字。"""
    return len(_DECORATION_RE.sub("", _DIGITS_RE.sub("", text)))


def _frequency_key(text: str) -> str:
    """页眉页脚的频率统计键。

    页码每页都不同，直接按原文统计频率永远只有 1 次、一条都剔不掉。折掉数字之后
    ``第 1 页`` 与 ``第 12 页`` collapse 成同一个键，才数得出"这东西每页都有"。

    但**只在这一行像页码时才折**。无条件折数字会把"位置在边缘、内容各页不同、
    差异恰好只在数字上"的正文也 collapse 成同一个键——``各页不同的边缘内容
    1/2/3`` 会变成一个键、计数 3、被当成页脚剔掉。这两个要求（剔页码 / 留正文）
    在纯数字折叠下是互斥的，判据只能来自别处。

    这里用"数字之外还剩几个实字"：页码是短模板 + 一个变化的数字，正文不是。
    残余超过阈值就退回精确匹配——那样它只有跨页**逐字重复**才会被剔，
    而逐字重复的边缘行本来就该当页眉处理（``公司内部资料`` 走的就是这条）。
    """
    stripped = text.strip()
    if _digit_residual(stripped) <= _PAGE_NUMBER_RESIDUAL_MAX:
        return _DIGITS_RE.sub("#", stripped)
    return stripped


def _strip_running_heads(pages: list[list[_Line]], heights: list[float]) -> int:
    """剔除页眉页脚。返回剔掉的行数。

    判据是**跨页重复**，不是位置——只按位置剔会把首页正文的第一行也剔掉。
    单页文档不做：一页看不出什么叫"重复出现"。
    """
    if len(pages) < max(2, settings.INGEST_HEADER_FOOTER_MIN_PAGES):
        return 0

    zone_counts: Counter[str] = Counter()
    for lines, height in zip(pages, heights):
        if height <= 0:
            continue
        margin = height * _MARGIN_ZONE
        seen: set[str] = set()
        for line in lines:
            if line.top <= margin or line.top >= height - margin:
                seen.add(_frequency_key(line.text))
        zone_counts.update(seen)

    threshold = max(2, settings.INGEST_HEADER_FOOTER_MIN_PAGES)
    repeated = {key for key, count in zone_counts.items() if count >= threshold}
    if not repeated:
        return 0

    removed = 0
    for index, (lines, height) in enumerate(zip(pages, heights)):
        if height <= 0:
            continue
        margin = height * _MARGIN_ZONE
        kept = []
        for line in lines:
            in_zone = line.top <= margin or line.top >= height - margin
            if in_zone and _frequency_key(line.text) in repeated:
                removed += 1
                continue
            kept.append(line)
        pages[index] = kept
    return removed


def _assign_heading_levels(pages: list[list[_Line]]) -> int:
    """按字号给行标标题层级。返回识别出的标题行数。

    正文字号取**按字符数加权**的众数，不是按行数：一份文档里标题行数可能比某个
    小字号脚注的行数还多，按行数投票会把正文认成标题。
    """
    weighted: Counter[float] = Counter()
    for lines in pages:
        for line in lines:
            weighted[line.size] += len(line.text)
    if not weighted:
        return 0

    body_size = weighted.most_common(1)[0][0]
    candidate_sizes = sorted(
        {
            line.size
            for lines in pages
            for line in lines
            if line.size > body_size * _HEADING_SIZE_RATIO
            and len(line.text) <= _HEADING_MAX_CHARS
            and line.text[-1] not in _SENTENCE_END
        },
        reverse=True,
    )
    if not candidate_sizes:
        return 0

    # 最大的字号是 #，往下依次 ##、###……超过 6 级都算 ######
    level_of = {size: min(6, index + 1) for index, size in enumerate(candidate_sizes)}
    count = 0
    for lines in pages:
        for line in lines:
            level = level_of.get(line.size)
            if (
                level is not None
                and len(line.text) <= _HEADING_MAX_CHARS
                and line.text[-1] not in _SENTENCE_END
            ):
                line.level = level
                count += 1
    return count


def _render(pages: list[list[_Line]]) -> str:
    """把分好级的行渲染成 Markdown，并把被折行/翻页切断的段落接回去。

    合并的条件是三个一起成立：上一行顶到了右边界（``full``，说明那句话没写完）、
    上一块不是标题、当前行不是列表项或新标题。少任何一条都会过度合并——只看
    "没有句末标点"会把整页粘成一段，连列表和表格行一起。
    """
    blocks: list[str] = []
    previous_full = False
    for lines in pages:
        for line in lines:
            if line.level:
                blocks.append(f"{'#' * line.level} {line.text}")
                previous_full = False
                continue
            if (
                blocks
                and previous_full
                and not blocks[-1].startswith("#")
                and blocks[-1][-1] not in _SENTENCE_END
                and not _LIST_HEAD_RE.match(line.text)
            ):
                glue = " " if _needs_space(blocks[-1], line.text) else ""
                blocks[-1] = f"{blocks[-1]}{glue}{line.text}"
            else:
                blocks.append(line.text)
            previous_full = line.full
    return "\n\n".join(blocks)


def structure_backend_available() -> bool:
    """pdfplumber 装上了吗。启动校验用它。

    单独暴露一个函数而不是让调用方 try import：缺依赖和"这个 PDF 解不了"是两回事，
    前者会让**每一份** PDF 都静默退回无结构抽取——同一份文档在两台机器上切出不同
    的块，而 eval 的结论就依赖这个。所以它必须在启动时就拦住，不能等上传时才发现。
    """
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        return False
    return True


def extract_pdf(content: bytes) -> PdfExtraction:
    """抽取 PDF 并恢复结构。失败时回退 PyPDF2 并记一条 warning。

    回退是运行时容错，不是依赖可选：pdfplumber 是必需依赖（``main.py`` 启动时
    校验），但单个畸形 PDF 让它抛异常时，退回纯文本抽取仍然比整个上传 500 好。
    """
    warnings: list[str] = []
    try:
        import pdfplumber
    except ImportError:
        # 走到这里说明启动校验被绕过了（比如直接调用脚本）。记一条显眼的
        # warning，让报告里能看出"这批文档根本没走结构恢复"。
        logger.error("pdfplumber not installed; PDF structure recovery is OFF")
        warnings.append("pdfplumber_missing")
        return extract_pdf_plain(content, warnings)

    try:
        page_lines: list[list[_Line]] = []
        heights: list[float] = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                words = page.extract_words(extra_attrs=["size"]) or []
                page_lines.append(_group_lines(words))
                heights.append(_as_float(page.height))
    except Exception as exc:
        logger.warning("pdfplumber extraction failed: %s", type(exc).__name__)
        warnings.append(f"pdfplumber_failed:{type(exc).__name__}")
        return extract_pdf_plain(content, warnings)

    pages = len(page_lines)
    if not any(page_lines):
        # 有页面但一个词都没抽到：几乎一定是扫描件（只有图像层）。
        # 这不是异常，所以必须靠 warning + 入库自检兜住，否则它会静默变成
        # 一篇 chunks=0 的 indexed 文档。
        warnings.append("no_text_layer")
        return PdfExtraction(text="", pages=pages, warnings=warnings)

    removed = _strip_running_heads(page_lines, heights)
    if removed:
        warnings.append(f"running_heads_removed:{removed}")

    headings = _assign_heading_levels(page_lines)
    if headings:
        warnings.append(f"headings_recovered:{headings}")
    else:
        # 没有标题就意味着 chunking 的 heading_path 仍然恒为空，
        # 「章节边界优先」对这份文档依旧不生效。值得记下来。
        warnings.append("no_headings_detected")

    return PdfExtraction(
        text=clean_text(_render(page_lines)), pages=pages, warnings=warnings
    )


def extract_pdf_plain(content: bytes, warnings: list[str]) -> PdfExtraction:
    """PyPDF2 纯文本抽取。``INGEST_PDF_STRUCTURE=false`` 的对照组，也是回退路径。"""
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        raise ValueError("PDF 解析需要安装 PyPDF2 库")

    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        pages = len(reader.pages)
    except Exception as exc:
        # 抛 ValueError 而不是让原始异常穿出去：路由把 ValueError 转成 400、
        # 其它异常转成 500，而"这个文件不是有效的 PDF"是用户错误不是服务错误。
        logger.warning("pypdf2 extraction failed: %s", type(exc).__name__)
        raise ValueError("无法解析该 PDF，文件可能已损坏或加了密") from exc

    if not text.strip():
        warnings.append("no_text_layer")
    return PdfExtraction(
        text=clean_text(text) if settings.INGEST_CLEAN else text,
        pages=pages,
        backend="pypdf2",
        warnings=warnings,
    )
