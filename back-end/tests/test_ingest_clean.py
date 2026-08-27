"""摄取层清洗:解码、PDF 结构恢复、入库自检。

这些用例都对着一类**不抛异常的失败**:文档进了库、状态是 indexed、界面上一切正常,
只是永远检索不到。所以断言的重点不是"函数返回了什么",而是"下游那两条召回通道
还认不认得出这段文本"——比如中文之间不能插空格(CJK bigram 会全废)、全角数字要
折成半角(lexical 探针考的就是 429 这类字面命中)。
"""
from __future__ import annotations

import pytest

from config import settings
from services import ingest_clean as ic
from services.retrieval_index import tokenize


# ========== 解码 ==========


def test_utf8_wins_before_sniffing():
    """utf-8 严格解通就一定是 utf-8;交给嗅探器反而会被短文本骗。"""
    result = ic.sniff_decode("报销标准".encode("utf-8"))

    assert result.text == "报销标准"
    assert result.encoding == "utf-8"
    assert result.replacement_ratio == 0.0


def test_bom_is_consumed():
    """BOM 留在正文里会变成第一个字符,而首行往往是 `# 标题`——
    `\\ufeff# 标题` 匹配不上 chunking 的 _HEADING_RE,整份文档的标题层级就没了。"""
    raw = b"\xef\xbb\xbf" + "# 报销政策".encode("utf-8")

    assert ic.sniff_decode(raw).text == "# 报销政策"


def test_gbk_decoded_via_encoding_hint():
    """这条是编码先验存在的理由:同一串字节在 GB18030 / EUC-KR 下都能严格解通,
    实测 charset-normalizer 会把这段 GBK 短句判成 EUC-KR、解出一串谚文。"""
    result = ic.sniff_decode("报销标准与差旅费用规定".encode("gbk"))

    assert result.text == "报销标准与差旅费用规定"
    assert result.encoding == "gb18030"


def test_encoding_hint_can_be_disabled(monkeypatch):
    """关掉先验就回到"让嗅探器猜"——这里只断言它不再走 gb18030,
    不断言它猜成什么:那取决于嗅探器版本,钉死会变成一个脆弱的用例。"""
    monkeypatch.setattr(settings, "INGEST_ENCODING_HINTS", "")

    assert ic.sniff_decode("报销标准".encode("gbk")).encoding != "gb18030"


def test_empty_bytes_are_not_an_error():
    result = ic.sniff_decode(b"")

    assert result.text == ""
    assert result.replacement_ratio == 0.0


# ========== 可读率 ==========


def test_readable_ratio_catches_mojibake():
    broken = "报销标准".encode("gbk").decode("utf-8", errors="replace")

    assert ic.readable_ratio(broken) < settings.INGEST_MIN_TEXT_RATIO
    assert ic.readable_ratio("报销标准") == 1.0


def test_readable_ratio_of_empty_text_is_one():
    """空是另一种失败(切不出块),由 no_chunks 单独判。
    这里返回 0 会让两种原因在告警里混成一个。"""
    assert ic.readable_ratio("") == 1.0
    assert ic.readable_ratio("   \n\t ") == 1.0


def test_mojibake_survives_tokenize_check():
    """这是整条链路上最贵的那个静默失败:替换符既不是拉丁词元也不是 CJK,
    tokenize 返回空 → BM25 建索引时 `if not tokens: continue` 跳过整块 →
    稀疏通道彻底看不见这篇文档,而状态还是 indexed。"""
    broken = "报销标准与差旅费用".encode("gbk").decode("utf-8", errors="replace")

    assert tokenize(broken) == []
    assert tokenize("报销标准与差旅费用") != []


# ========== 文本规整 ==========


def test_fullwidth_digits_are_folded():
    """全角 ４２９ 和半角 429 在 tokenize 里是不同的词元,
    而金标准里 lexical 探针考的正是 429 / P8 这类字面命中。"""
    cleaned = ic.clean_text("错误码 ４２９ 表示限流")

    assert "429" in cleaned
    assert "429" in tokenize(cleaned)


def test_zero_width_inside_a_word_is_removed():
    """夹在词中间的 U+200B 会把一个词元切成两个。"""
    assert ic.clean_text("RESOURCE​_EXHAUSTED") == "RESOURCE_EXHAUSTED"
    assert "resource_exhausted" in tokenize(ic.clean_text("RESOURCE​_EXHAUSTED"))


def test_control_chars_and_blank_runs_collapse():
    assert ic.clean_text("a\x0c\n\n\n\n\nb") == "a\n\nb"
    assert ic.clean_text("行尾有空格   \n下一行") == "行尾有空格\n下一行"


def test_crlf_is_normalized():
    assert ic.clean_text("第一行\r\n第二行\r第三行") == "第一行\n第二行\n第三行"


def test_clean_text_is_idempotent():
    messy = "错误码 ４２９\r\n\n\n\nRESOURCE​_EXHAUSTED  "
    once = ic.clean_text(messy)

    assert ic.clean_text(once) == once


# ========== 词内空格 ==========


def test_never_inserts_space_between_cjk():
    """PDF 里中文常常一个字一个 word。无条件用空格拼会把整篇中文变成
    「每 个 字 之 间 都 有 空 格」,而 CJK bigram 取的是相邻字符——全废。"""
    assert ic._needs_space("销", "标") is False
    assert ic._needs_space("报销", "P8") is False
    assert ic._needs_space("429", "元") is False


def test_inserts_space_between_latin_words():
    assert ic._needs_space("hello", "world") is True
    assert ic._needs_space("RESOURCE_", "EXHAUSTED") is True


def _word(text: str, top: float, size: float = 10.0, x0: float = 50.0, x1: float | None = None):
    return {
        "text": text,
        "top": top,
        "size": size,
        "x0": x0,
        "x1": x1 if x1 is not None else x0 + len(text) * size,
    }


def test_tight_gap_does_not_get_a_space():
    """间隙小于字号的四分之一说明这两段本来是连着的,是按字距被切开的。
    补空格就等于凭空在 RESOURCE_EXHAUSTED 中间插一个空格。"""
    words = [
        _word("RESOURCE_", 100.0, x0=50.0, x1=140.0),
        _word("EXHAUSTED", 100.0, x0=141.0),  # 间隙 1pt < 10 * 0.28
    ]

    assert ic._join_words(words) == "RESOURCE_EXHAUSTED"


def test_wide_gap_gets_a_space():
    words = [
        _word("hello", 100.0, x0=50.0, x1=100.0),
        _word("world", 100.0, x0=120.0),  # 间隙 20pt
    ]

    assert ic._join_words(words) == "hello world"


# ========== 行分组与标题层级 ==========


def test_lines_group_by_vertical_position():
    words = [_word("左", 100.0, x0=50.0), _word("右", 100.5, x0=80.0), _word("下一行", 130.0)]

    lines = ic._group_lines(words)

    assert [line.text for line in lines] == ["左右", "下一行"]


def test_heading_levels_follow_font_size():
    """标题层级只能靠字号推——这正是必须用 pdfplumber 而不是 PyPDF2 的原因,
    后者返回的字符串里没有字号,怎么正则都推不出层级。"""
    pages = [
        ic._group_lines(
            [
                _word("报销政策", 100.0, size=18.0),
                _word("差旅标准", 140.0, size=14.0),
                _word("这是一段正文内容,足够长以便成为字号众数。", 180.0, size=10.0),
                _word("另一段同样字号的正文内容,继续拉高众数权重。", 220.0, size=10.0),
            ]
        )
    ]

    count = ic._assign_heading_levels(pages)
    rendered = ic._render(pages)

    assert count == 2
    assert "# 报销政策" in rendered
    assert "## 差旅标准" in rendered


def test_body_size_is_weighted_by_characters_not_lines():
    """按行数投票会出错:标题行数可能比某个字号的正文行数还多。"""
    pages = [
        ic._group_lines(
            [
                _word("标一", 100.0, size=20.0),
                _word("标二", 130.0, size=20.0),
                _word("标三", 160.0, size=20.0),
                _word("这一行虽然只有一行,但字符数远多于上面三个短标题之和。", 200.0, size=10.0),
            ]
        )
    ]

    ic._assign_heading_levels(pages)

    assert [line.level for line in pages[0]] == [1, 1, 1, 0]


def test_long_line_is_not_a_heading():
    """大字号的整段引言不该被当成标题。"""
    long_text = "这是一段用大字号排版的引言" * 8
    pages = [
        ic._group_lines(
            [
                _word(long_text, 100.0, size=18.0),
                _word("正常正文内容,用来当字号众数。", 200.0, size=10.0),
            ]
        )
    ]

    ic._assign_heading_levels(pages)

    assert pages[0][0].level == 0


# ========== 页眉页脚 ==========


def _paged(page_count: int, header: str = "公司内部资料") -> tuple[list, list]:
    pages, heights = [], []
    for number in range(1, page_count + 1):
        words = [
            _word(header, 20.0, size=8.0),
            _word(f"第 {number} 页", 760.0, size=8.0),
            _word(f"这是第 {number} 页的正文内容。", 300.0),
        ]
        pages.append(ic._group_lines(words))
        heights.append(800.0)
    return pages, heights


def test_page_numbers_are_removed_by_digit_folding():
    """页码每页都不同,按原文统计频率永远只有 1 次、一条都剔不掉。
    把数字折成 # 之后「第 1 页」与「第 12 页」才 collapse 成同一个键。"""
    pages, heights = _paged(3)

    removed = ic._strip_running_heads(pages, heights)
    rendered = ic._render(pages)

    assert removed == 6  # 3 页 × (页眉 + 页脚)
    assert "公司内部资料" not in rendered
    # 按**整行**判断页码是否剔掉,不能用子串:保留下来的正文是"这是第 1 页的正文
    # 内容。",它本身就含「第 1 页」。原来写成 `"第 1 页" not in rendered` 于是
    # 恒假——功能一直是对的,红的是断言。
    lines = [line.strip() for line in rendered.splitlines()]
    assert not any(line == f"第 {number} 页" for number in (1, 2, 3) for line in lines)
    assert "这是第 1 页的正文内容。" in rendered


def test_single_page_keeps_everything():
    """一页看不出什么叫"重复出现";按位置单独判会把首页正文第一行也剔掉。"""
    pages, heights = _paged(1)

    assert ic._strip_running_heads(pages, heights) == 0


def test_below_threshold_keeps_everything(monkeypatch):
    monkeypatch.setattr(settings, "INGEST_HEADER_FOOTER_MIN_PAGES", 5)
    pages, heights = _paged(3)

    assert ic._strip_running_heads(pages, heights) == 0


def test_body_text_in_margin_zone_survives():
    """只有跨页重复的才剔。位置在边缘但内容各页不同的,是正文。

    这条与 ``test_page_numbers_are_removed_by_digit_folding`` 在纯数字折叠下是
    **互斥**的:``各页不同的边缘内容 1/2/3`` 折完是一个键、计数 3,与页码无从区分。
    判据因此不能只看"折完是否重复",还要看这一行像不像页码——见
    ``_frequency_key`` 与下面几条残余判据的测试。
    """
    pages, heights = [], []
    for number in (1, 2, 3):
        pages.append(ic._group_lines([_word(f"各页不同的边缘内容 {number}", 20.0, size=8.0)]))
        heights.append(800.0)

    assert ic._strip_running_heads(pages, heights) == 0


@pytest.mark.parametrize(
    "text",
    ["第 1 页", "- 5 -", "5", "Page 12 of 30", "第 3 页 / 共 10 页"],
)
def test_page_number_shapes_get_digits_folded(text):
    """页码形状:数字之外只剩极短模板,应当折叠成 # 才数得出跨页重复。"""
    assert "#" in ic._frequency_key(text)


@pytest.mark.parametrize(
    "text",
    [
        "各页不同的边缘内容 1",
        "这是第 1 页的正文内容。",
        "2026 年第三季度营收同比增长 18%",
        "表 1 显示了各部门的人员编制情况",
    ],
)
def test_body_shapes_keep_their_digits(text):
    """正文形状:残余太长,不折叠——于是只有逐字重复才会被当页眉剔掉。"""
    assert ic._frequency_key(text) == text.strip()


def test_exact_repeats_are_still_stripped_without_folding():
    """不含数字的页眉照旧靠精确重复剔除,残余判据不影响它。"""
    key = ic._frequency_key("Acme 科技内部资料 — 未经许可不得外传")
    assert key == "Acme 科技内部资料 — 未经许可不得外传"
    pages, heights = [], []
    for _ in range(3):
        pages.append(
            ic._group_lines([_word("Acme 科技内部资料 — 未经许可不得外传", 20.0, size=8.0)])
        )
        heights.append(800.0)
    assert ic._strip_running_heads(pages, heights) == 3


# ========== 段落合并 ==========


def _line(text: str, *, full: bool, level: int = 0) -> ic._Line:
    return ic._Line(text=text, size=10.0, top=0.0, right=500.0, full=full, level=level)


def test_wrapped_paragraph_is_rejoined():
    pages = [[_line("员工出差需在五个工作日内提交申请并附上", full=True)],
             [_line("完整的发票与行程单。", full=False)]]

    assert ic._render(pages) == "员工出差需在五个工作日内提交申请并附上完整的发票与行程单。"


def test_short_line_does_not_absorb_the_next():
    """只看"末尾没有句末标点"会把整页粘成一段,连列表和表格行一起。
    行末没顶到右边界就说明这段话写完了。"""
    pages = [[_line("招待标准另行规定", full=False), _line("酒水另有比例限制", full=False)]]

    assert ic._render(pages) == "招待标准另行规定\n\n酒水另有比例限制"


def test_list_item_is_never_absorbed():
    pages = [[_line("以下情形不予报销", full=True), _line("- 未附发票的支出", full=False)]]

    assert ic._render(pages) == "以下情形不予报销\n\n- 未附发票的支出"


def test_heading_breaks_the_paragraph():
    pages = [[_line("上一段没写完", full=True), _line("差旅标准", full=True, level=2)]]

    assert ic._render(pages) == "上一段没写完\n\n## 差旅标准"


def test_sentence_end_breaks_the_paragraph():
    pages = [[_line("这一句已经写完了。", full=True), _line("下一段开始", full=False)]]

    assert ic._render(pages) == "这一句已经写完了。\n\n下一段开始"


# ========== 抽取入口 ==========


def test_broken_pdf_raises_value_error():
    """畸形 PDF：pdfplumber 失败后退回 PyPDF2，两个都解不了就抛 ValueError。

    必须是 ValueError 而不是别的异常——路由把 ValueError 转 400、其它转 500，
    而"这个文件不是有效的 PDF"是用户错误。也必须**抛**而不是返回空文本，
    否则它会静默落成一篇 chunks=0 的 indexed 文档。
    """
    with pytest.raises(ValueError):
        ic.extract_pdf(b"not a pdf at all")


def test_scanned_pdf_is_reported_as_no_text_layer():
    """有页面但一个词都抽不到 = 扫描件。它不抛异常,所以必须靠 warning
    加入库自检兜住,否则会静默变成一篇 chunks=0 的 indexed 文档。"""
    extraction = ic.PdfExtraction(text="", pages=3, warnings=["no_text_layer"])

    assert extraction.text == ""
    assert "no_text_layer" in extraction.warnings
