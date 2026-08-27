""".docx 解析：段落、表格、标题层级，以及它们为什么必须是这个形状。

这套测试的重点不在"能不能读出文字"——python-docx 干的就是这个。重点在三件
**决定检索质量**的事，而它们全都不抛异常、只表现为"检索不到"：

1. **标题渲染成 Markdown。** 这不是格式偏好：``chunking`` 有两套机制吃 `#` 标题
   （给每块加标题路径、章节边界优先于填满 max_tokens），而
   ``_looks_like_markdown`` 会回退扫前 200 行找 `#`。渲染不出标题就等于这两套
   机制对所有 Word 文档静默失效——heading_path 恒为空，而没有任何报错。
2. **按文档流顺序遍历。** python-docx 的 ``paragraphs`` 与 ``tables`` 是两个独立
   列表，分别遍历会把所有表格搬到文末，于是表格脱离它所属的小节。
   "报销标准"那张表会挂到最后一个标题下面，而标题路径正是按出现顺序算的。
3. **中文版 Word 的样式名是本地化的。** 只认 ``Heading N`` 的话，一份中文
   Word 文档一个标题都识别不出来——这是最容易漏、也最容易在演示时才发现的一条。

第 3 条尤其值得一条专门的测试：``python-docx`` 的 ``add_heading`` 写的是英文样式
名，所以**光靠造夹具永远测不到中文分支**。
"""
from __future__ import annotations

import io

import pytest

from services import ingest_clean
from services.ingest_clean import _docx_heading_level, extract_docx
from services.knowledge_service import parse_document

docx = pytest.importorskip("docx", reason="python-docx 未安装")


def _build(*, headings=True, table=False, title=False, empty=False) -> bytes:
    """造一份 .docx。默认是"两级标题 + 正文"的最常见形状。"""
    document = docx.Document()
    if empty:
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()
    if title:
        document.add_paragraph("封面标题", style="Title")
    if headings:
        document.add_heading("差旅报销制度", level=1)
    document.add_paragraph("本制度适用于全体员工。")
    if headings:
        document.add_heading("报销时限", level=2)
    document.add_paragraph("出差结束后三十个自然日内提交。")
    if table:
        rendered = document.add_table(rows=2, cols=2)
        rendered.cell(0, 0).text = "项目"
        rendered.cell(0, 1).text = "标准"
        rendered.cell(1, 0).text = "住宿"
        rendered.cell(1, 1).text = "每晚 500 元"
    if headings:
        document.add_heading("审批流程", level=2)
    document.add_paragraph("由直属上级审批。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ========== 标题层级 ==========


@pytest.mark.parametrize(
    "style_name,expected",
    [
        ("Heading 1", 1),
        ("Heading 3", 3),
        ("heading 6", 6),
        # 中文版 Word。造夹具测不到这一支（add_heading 写的是英文名），
        # 所以只能直接测这个函数——而漏掉它的后果是中文文档零标题。
        ("标题 1", 1),
        ("标题 2", 2),
        # 超出 h6 收敛到 6：Markdown 没有 h7，而 Word 支持到 9
        ("Heading 9", 6),
        # 封面元素不是章节标题：映射成 h1 会让整篇挂在一个标题下，等于没层级
        ("Title", 0),
        ("Subtitle", 0),
        ("Normal", 0),
        ("List Bullet", 0),
        ("", 0),
    ],
)
def test_heading_level_from_style_name(style_name, expected):
    assert _docx_heading_level(style_name) == expected


def test_headings_render_as_markdown_hashes():
    """`#` 的数量必须等于 Word 里的层级，否则标题路径的嵌套关系是错的。"""
    result = extract_docx(_build())
    assert "# 差旅报销制度" in result.text
    assert "## 报销时限" in result.text
    assert "## 审批流程" in result.text


def test_title_style_is_not_treated_as_a_heading():
    result = extract_docx(_build(title=True))
    assert "封面标题" in result.text
    assert "# 封面标题" not in result.text


# ========== 文档流顺序：表格必须留在它所属的小节里 ==========


def test_table_stays_in_document_flow_order():
    """这条锁住整个遍历方式。

    分别遍历 paragraphs 和 tables 会让表格跑到文末——文本仍然"都在"，
    所以只断言"表格内容存在"的测试照样绿，而标题路径已经错了。
    """
    text = extract_docx(_build(table=True)).text
    table_at = text.index("| 项目 | 标准 |")
    assert text.index("## 报销时限") < table_at, "表格跑到了它所属小节之前"
    assert table_at < text.index("## 审批流程"), "表格跑到了下一个小节之后"


def test_table_renders_as_a_markdown_table_with_a_header_row():
    """保留表格形状而不是拉平：表头是每一行都需要的上下文。

    拉平之后"每晚 500 元"这个单元格会脱离"标准"这个列名，
    检索命中了也答不出"住宿标准是多少"。
    """
    text = extract_docx(_build(table=True)).text
    assert "| 项目 | 标准 |" in text
    assert "| --- | --- |" in text
    assert "| 住宿 | 每晚 500 元 |" in text


def test_empty_table_is_skipped_rather_than_rendered_as_pipes():
    """全空的表格渲染出来是一堆 `|  |  |`——纯噪声，会稀释这一块的 embedding。"""
    document = docx.Document()
    document.add_heading("小节", level=1)
    document.add_table(rows=2, cols=2)  # 一个字都不填
    document.add_paragraph("正文。")
    buffer = io.BytesIO()
    document.save(buffer)

    result = extract_docx(buffer.getvalue())
    assert "|" not in result.text
    assert "tables_extracted" not in " ".join(result.warnings)


def test_cell_newlines_are_flattened_so_rows_stay_on_one_line():
    """单元格里可以有换行，而 Markdown 表格的一行必须是一行。

    不压平的话那一行会从中间断开，后面的列全部错位——表格结构塌了，
    而这恰恰是保留表格形状想换来的东西。
    """
    document = docx.Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "标准"
    table.cell(1, 0).text = "住宿"
    table.cell(1, 1).text = "工作日 500 元\n节假日 800 元"
    buffer = io.BytesIO()
    document.save(buffer)

    text = extract_docx(buffer.getvalue()).text
    assert "| 住宿 | 工作日 500 元 节假日 800 元 |" in text


# ========== warnings：静默失效都靠它冒泡 ==========


def test_headings_recovered_is_counted():
    result = extract_docx(_build())
    assert "headings_recovered:3" in result.warnings


def test_no_headings_detected_is_reported():
    """作者用加粗大字号代替标题样式——很常见，而它让章节边界优先彻底失效。

    沿用 PDF 那套词汇（``no_headings_detected``），这样入库自检和界面
    不必分格式各写一套判断。
    """
    result = extract_docx(_build(headings=False))
    assert "no_headings_detected" in result.warnings
    assert not [w for w in result.warnings if w.startswith("headings_recovered")]


def test_tables_extracted_is_counted():
    result = extract_docx(_build(table=True))
    assert "tables_extracted:1" in result.warnings


def test_empty_document_warns_instead_of_raising():
    """一个段落一张表都抽不到，通常是内容全在文本框/图片里（python-docx 读不到）。

    它是一篇**合法**的 docx，所以不抛异常——但也不能静默变成一篇 chunks=0 的
    indexed 文档，那是这个仓库里最贵的一类失败。
    """
    result = extract_docx(_build(empty=True))
    assert "no_extractable_text" in result.warnings
    assert result.text == ""


# ========== 坏输入 ==========


def test_legacy_doc_renamed_to_docx_raises_with_actionable_wording():
    """把老的 .doc 改名成 .docx 是最常见的坏输入。

    抛 ValueError 而不是让原始异常穿出去：路由把 ValueError 转 400、其它转 500，
    而"这不是有效的 docx"是用户错误。文案必须说清楚要另存为 .docx，
    否则用户会反复重传同一个文件。
    """
    with pytest.raises(ValueError) as excinfo:
        extract_docx(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 old OLE2 doc")
    assert ".docx" in str(excinfo.value)


def test_missing_dependency_message_is_actionable(monkeypatch):
    """依赖没装时的文案。这条路径在 CI 里不会走到，但它是部署新环境时的第一堵墙。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docx":
            raise ImportError("no docx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ValueError, match="python-docx"):
        extract_docx(b"PK\x03\x04whatever")


# ========== 接线：parse_document 与 chunking ==========


def test_parse_document_dispatches_docx_and_reports_backend():
    """``backend`` 会进库、也给界面看：解析失败的几种形态都不抛异常，
    排查时它是第一手信息。"""
    parsed = parse_document("差旅制度.docx", _build(table=True))
    assert parsed.backend == "python-docx"
    assert "headings_recovered:3" in parsed.warnings
    assert "# 差旅报销制度" in parsed.text


def test_docx_chunks_carry_heading_paths():
    """整条链路的收益就在这一条上。

    渲染成 Markdown 的全部理由是让 ``chunking`` 白拿标题路径与章节边界优先。
    这条断言就是那个收益本身——它红了说明前面所有工作没有产生效果，
    而中间任何一步都不会报错。
    """
    from services.chunking import split_document

    parsed = parse_document("差旅制度.docx", _build(table=True))
    chunks = split_document(parsed.text, "差旅制度.docx")

    assert chunks
    assert all(chunk.heading_path for chunk in chunks), "有块的标题路径是空的"
    assert any(
        chunk.heading_path == "差旅报销制度 > 报销时限" for chunk in chunks
    ), [chunk.heading_path for chunk in chunks]


def test_docx_without_headings_still_chunks_without_crashing():
    """无标题是合法输入（只是检索质量差），不能让入库整个失败。"""
    from services.chunking import split_document

    parsed = parse_document("无标题.docx", _build(headings=False))
    assert split_document(parsed.text, "无标题.docx")


def test_ingest_clean_toggle_is_respected(monkeypatch):
    """``INGEST_CLEAN=false`` 是改动前行为的对照组，docx 这条路也得听它。"""
    monkeypatch.setattr(ingest_clean.settings, "INGEST_CLEAN", False)
    assert extract_docx(_build()).text
