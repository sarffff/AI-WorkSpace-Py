""".xlsx 解析：为什么每行自带列名，而不是照抄 docx 的 Markdown 表格。

块 4 只有一个真实取舍，这个文件的大半都在锁住它。

docx 的表格渲染成 Markdown 表格（表头一行 + 数据行），对一份几行的制度表是对的。
Excel 不行：一张表动辄几百行，而 ``max_tokens`` 默认 320，几十行就要断开。断开
之后从第二块起全是裸数据行——"500" 脱离了"住宿上限"这个列名，检索命中了也答不出
"一线城市住宿标准是多少"。更糟的是 Markdown 行会被**从中间切开**，于是块的开头
是半行 `85 |`。

所以每行渲染成 ``列名: 值 | 列名: 值``：

- 每一行都自足，切在哪都不丢上下文；
- 行之间空行分隔 → ``chunking._parse_blocks`` 遇空行 flush，每行成为独立 block；
- 行内不用句末标点（分隔用 `|`，冒号是全角不在 ``_SENTENCE_RE`` 里）
  → ``_split_units`` 的句子切分不会从行中间切开。

代价是列名在每行重复一次，token 更高。这笔交易是划算的：冗余只是提示词成本，
而丢上下文是**答不出来**。``test_every_chunk_is_self_sufficient`` 连同它的
Markdown 对照组一起，就是这笔交易的证据。
"""
from __future__ import annotations

import io
from datetime import date, datetime

import pytest

from services import ingest_clean
from services.ingest_clean import (
    _XLSX_MAX_ROWS,
    _xlsx_cell_text,
    _xlsx_header_row,
    extract_xlsx,
)
from services.knowledge_service import parse_document

openpyxl = pytest.importorskip("openpyxl", reason="openpyxl 未安装")


def _build(rows: list[list], *, title: str = "住宿标准", extra: dict | None = None) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    for name, extra_rows in (extra or {}).items():
        other = workbook.create_sheet(name)
        for row in extra_rows:
            other.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


_BASIC = [
    ["城市等级", "住宿上限", "生效日期"],
    ["一线城市", 500.0, datetime(2026, 1, 1)],
    ["二线城市", 350.0, datetime(2026, 1, 1)],
]


# ========== 核心取舍：每块都要自足 ==========


def test_rows_render_as_key_value_pairs_not_a_markdown_table():
    text = extract_xlsx(_build(_BASIC)).text
    assert "城市等级: 一线城市 | 住宿上限: 500" in text
    # 不能出现 Markdown 表格的分隔行，那说明照抄了 docx 那条路
    assert "| --- |" not in text


def test_rows_are_separated_by_blank_lines_so_each_becomes_its_own_block():
    """空行是"每行独立成块"的机制来源，见 chunking._parse_blocks。"""
    text = extract_xlsx(_build(_BASIC)).text
    assert "生效日期: 2026-01-01\n\n城市等级: 二线城市" in text


def test_rows_contain_no_sentence_punctuation_that_would_split_them():
    """行内不能有 ``_SENTENCE_RE`` 认的句末标点，否则超长块会被从行中间切开。"""
    from services.chunking import _SENTENCE_RE

    text = extract_xlsx(_build(_BASIC)).text
    for line in text.splitlines():
        if line.startswith("##") or not line.strip():
            continue
        assert len(_SENTENCE_RE.split(line)) == 1, f"这一行会被句子切分拆开：{line!r}"


def test_every_chunk_is_self_sufficient_unlike_a_markdown_table():
    """整个块 4 的取舍就在这一条上，连同它的对照组。

    40 行足以超过 max_tokens 触发多块。键值行渲染下每一块都带列名；
    同样数据渲染成 Markdown 表格时，只有第一块有表头。
    """
    from services.chunking import split_document

    rows = [["城市", "住宿上限", "餐补", "交通"]]
    rows += [[f"城市{index:02d}", 300 + index, 80 + index, 50 + index] for index in range(40)]
    parsed = parse_document("标准.xlsx", _build(rows, title="报销标准"))
    chunks = split_document(parsed.text, "标准.xlsx")

    assert len(chunks) > 1, "样本不足以触发切分，这条断言就失去意义了"
    assert all("住宿上限" in chunk.content for chunk in chunks), "有块丢了列名"

    # 对照组：同样的数据用 Markdown 表格，验证"照抄 docx 会坏"不是假想
    table = ["| 城市 | 住宿上限 | 餐补 | 交通 |", "| --- | --- | --- | --- |"]
    table += [f"| 城市{i:02d} | {300 + i} | {80 + i} | {50 + i} |" for i in range(40)]
    markdown_chunks = split_document("## 报销标准\n\n" + "\n".join(table), "t.md")
    with_header = sum(1 for chunk in markdown_chunks if "住宿上限" in chunk.content)
    assert with_header < len(markdown_chunks), (
        "Markdown 表格居然每块都带表头——那这个取舍的前提变了，回去重新评估"
    )


def test_sheet_names_become_headings_so_chunks_get_heading_paths():
    """工作表名是这批行的共同上下文（"住宿标准" vs "交通标准"）。"""
    from services.chunking import split_document

    content = _build(_BASIC, extra={"交通标准": [["项目", "标准"], ["市内交通", 100.0]]})
    parsed = parse_document("差旅.xlsx", content)
    paths = {chunk.heading_path for chunk in split_document(parsed.text, "差旅.xlsx")}
    assert paths == {"住宿标准", "交通标准"}


# ========== 表头识别 ==========


def test_header_row_is_the_densest_of_the_first_rows_not_just_the_first():
    """真实表格常常第一行是标题（只占 A1），表头在第二行。

    取错的后果不是报错，是每一行都带上错的列名——一整篇文档静默变成噪声。
    """
    rows = [["2026 年差旅标准"], ["城市等级", "住宿上限", "生效日期"]] + _BASIC[1:]
    text = extract_xlsx(_build(rows)).text
    assert "城市等级: 一线城市" in text
    assert "2026 年差旅标准: " not in text


def test_header_row_helper_returns_the_data_start_index():
    header, start = _xlsx_header_row([["标题"], ["A", "B", "C"], ["1", "2", "3"]])
    assert header == ["A", "B", "C"]
    assert start == 2


def test_header_row_ties_prefer_the_earlier_row():
    """并列时取更靠上的：表头一般在数据之上。"""
    _header, start = _xlsx_header_row([["A", "B"], ["1", "2"]])
    assert start == 1


def test_blank_header_cells_get_a_positional_name():
    """列名为空时不能渲染成 `: 值`——那个值会彻底脱离上下文。"""
    header, _start = _xlsx_header_row([["城市", "", "备注"]])
    assert header == ["城市", "第2列", "备注"]


# ========== 单元格取值 ==========


@pytest.mark.parametrize(
    "value,expected",
    [
        # 恒为零的时间部分是噪声，也会给 BM25 多出无意义词元
        (datetime(2026, 8, 24), "2026-08-24"),
        (datetime(2026, 8, 24, 9, 30), "2026-08-24 09:30:00"),
        (date(2026, 8, 24), "2026-08-24"),
        # 100.0 必须是 100，否则"每日 100 元"这类查询字面匹配不上
        (500.0, "500"),
        (500.5, "500.5"),
        (42, "42"),
        (None, ""),
        (True, "True"),
        ("  多余   空白 ", "多余 空白"),
    ],
)
def test_cell_text_normalization(value, expected):
    assert _xlsx_cell_text(value) == expected


def test_empty_cells_are_omitted_rather_than_rendered_as_bare_column_names():
    """稀疏表（很多可选列）在真实数据里很常见，`列名: ` 是纯噪声。"""
    rows = [["城市等级", "住宿上限", "生效日期"], ["其他", 250.0, None]]
    text = extract_xlsx(_build(rows)).text
    assert "城市等级: 其他 | 住宿上限: 250" in text
    assert "生效日期:" not in text


def test_fully_empty_rows_are_skipped():
    rows = [["城市", "上限"], [None, None], ["北京", 500]]
    text = extract_xlsx(_build(rows)).text
    assert text.count("城市:") == 1


# ========== warnings ==========


def test_sheet_and_row_counts_are_reported():
    result = extract_xlsx(_build(_BASIC))
    assert "sheets_extracted:1" in result.warnings
    assert "rows_extracted:2" in result.warnings


def test_row_truncation_is_visible_in_warnings():
    """静默丢掉后半张表的症状是"某些条目怎么都检索不到"，而文档状态是 indexed、
    chunks 也非零，看起来一切正常。这是这个仓库最不能接受的一类行为。"""
    rows = [["编号", "值"]] + [[index, index] for index in range(_XLSX_MAX_ROWS + 50)]
    result = extract_xlsx(_build(rows, title="明细"))
    truncation = [w for w in result.warnings if w.startswith("rows_truncated:")]
    assert truncation, result.warnings
    assert str(_XLSX_MAX_ROWS) in truncation[0]
    assert "明细" in truncation[0]


def test_empty_workbook_warns_instead_of_raising():
    """一张空工作簿是合法的 xlsx，不该抛异常；但也不能静默变成 chunks=0 的
    indexed 文档。"""
    result = extract_xlsx(_build([]))
    assert "no_extractable_text" in result.warnings
    assert result.text == ""


def test_sheets_with_no_data_do_not_count_toward_sheets_extracted():
    content = _build(_BASIC, extra={"空表": []})
    result = extract_xlsx(content)
    assert "sheets_extracted:1" in result.warnings


# ========== 坏输入与接线 ==========


def test_legacy_xls_renamed_raises_with_actionable_wording():
    with pytest.raises(ValueError) as excinfo:
        extract_xlsx(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 old OLE2 xls")
    assert ".xlsx" in str(excinfo.value)


def test_missing_dependency_message_is_actionable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("no openpyxl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ValueError, match="openpyxl"):
        extract_xlsx(b"PK\x03\x04whatever")


def test_parse_document_dispatches_xlsx_and_reports_backend():
    parsed = parse_document("差旅标准.xlsx", _build(_BASIC))
    assert parsed.backend == "openpyxl"
    assert "sheets_extracted:1" in parsed.warnings


def test_ingest_clean_toggle_is_respected(monkeypatch):
    monkeypatch.setattr(ingest_clean.settings, "INGEST_CLEAN", False)
    assert extract_xlsx(_build(_BASIC)).text


def test_xlsx_is_in_the_document_category_and_gated_by_a_signature():
    """接线断言：白名单收 xlsx，且签名表覆盖它。

    ``test_file_types_single_source`` 已经按命名约定盯着签名常量，这里补的是
    "查表时真的用上了"——常量存在但没进 _DOCUMENT_SIGNATURES 的话，
    改扩展名就能绕过。
    """
    from routers.attachment_router import _DOCUMENT_SIGNATURES
    from services import file_types

    assert "xlsx" in file_types.DOCUMENT
    assert "xlsx" in file_types.KNOWLEDGE
    assert _DOCUMENT_SIGNATURES["xlsx"] == b"PK\x03\x04"
