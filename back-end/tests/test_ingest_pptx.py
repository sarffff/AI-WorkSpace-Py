""".pptx 解析：标题、要点、表格、讲稿备注，以及它们为什么必须是这个形状。

重点不在"能不能读出文字"——python-pptx 干的就是这个。重点在三件**决定检索质量**
的事，它们全都不抛异常、只表现为"检索不到"：

1. **每页渲染成 Markdown 二级标题。** 同 docx/xlsx：``chunking`` 的两套机制吃 `#`
   标题（标题路径、章节边界优先），渲染不出标题就等于它们对所有幻灯片静默失效。
2. **讲稿备注也要抽。** 演讲者常把真正的解释写在备注里，幻灯片上只留一个标题。
   漏掉备注的话，这类 deck 会几乎抽不到东西——而文档状态是 indexed。
3. **标题不在正文里重复一遍。** 标题已经进了 heading，正文再列一次会让同一句话
   在同一个块里出现两遍，稀释 embedding 也污染 BM25。
"""
from __future__ import annotations

import io

import pytest

from services import file_types
from services.ingest_clean import extract_pptx
from services.knowledge_service import parse_document

pptx = pytest.importorskip("pptx", reason="python-pptx 未安装")


def _build(
    *,
    title: str | None = "差旅报销制度",
    bullets: list[str] | None = None,
    notes: str | None = None,
    table: bool = False,
    blank: bool = False,
) -> bytes:
    """造一份 .pptx。默认是"标题 + 两条要点"的最常见形状。"""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    if blank:
        # 版式 6 是空白版式：没有占位符，抽不到任何文字
        presentation.slides.add_slide(presentation.slide_layouts[6])
    else:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        if title is not None:
            slide.shapes.title.text = title
        body = slide.placeholders[1].text_frame
        for index, text in enumerate(bullets or ["本制度适用于全体员工。"]):
            if index == 0:
                body.text = text
            else:
                body.add_paragraph().text = text
        if notes is not None:
            slide.notes_slide.notes_text_frame.text = notes
        if table:
            shape = slide.shapes.add_table(
                2, 2, Inches(1), Inches(3), Inches(4), Inches(1)
            )
            cells = shape.table
            cells.cell(0, 0).text = "项目"
            cells.cell(0, 1).text = "标准"
            cells.cell(1, 0).text = "住宿"
            cells.cell(1, 1).text = "每晚 500 元"

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


# ========== 标题与要点 ==========


def test_slide_title_renders_as_markdown_heading():
    """每页一个二级标题，标题文字跟在页码后面——它是这一页要点的共同上下文。

    分隔用半角冒号：``clean_text`` 会把全角 ： 折成半角，断言跟着用半角。
    """
    result = extract_pptx(_build())
    assert "## 第 1 页: 差旅报销制度" in result.text


def test_body_bullets_are_extracted():
    result = extract_pptx(_build(bullets=["本制度适用于全体员工。", "出差结束后三十日内提交。"]))
    assert "本制度适用于全体员工。" in result.text
    assert "出差结束后三十日内提交。" in result.text


def test_title_is_not_duplicated_as_a_bullet():
    """标题已经进了 heading，正文里不该再以要点形式出现第二遍。"""
    result = extract_pptx(_build(title="差旅报销制度"))
    # heading 里出现一次，但不该有一个"- 差旅报销制度"的要点
    assert "- 差旅报销制度" not in result.text


# ========== 讲稿备注 ==========


def test_notes_are_extracted():
    """演讲者把解释写在备注里，漏掉它这类 deck 会几乎抽不到东西。

    备注正文避开全角冒号：``clean_text`` 会把它折成半角，而这里要验的是
    "备注被抽出来了"而不是全角折叠本身（那由 test_ingest_clean 管）。
    """
    result = extract_pptx(_build(notes="强调三十日是自然日不是工作日。"))
    assert "讲稿备注: 强调三十日是自然日不是工作日。" in result.text


def test_notes_only_slide_still_has_content():
    """幻灯片上只有标题、内容全在备注里，也算抽到了东西（不该报 no_extractable_text）。"""
    result = extract_pptx(_build(title="结论", bullets=[], notes="全部结论都在讲稿里。"))
    assert "no_extractable_text" not in result.warnings
    assert "全部结论都在讲稿里。" in result.text


# ========== 表格 ==========


def test_table_renders_as_markdown():
    result = extract_pptx(_build(table=True))
    assert "| 项目 | 标准 |" in result.text
    assert "| 住宿 | 每晚 500 元 |" in result.text


# ========== 边界与告警 ==========


def test_blank_deck_warns_no_extractable_text():
    """有幻灯片、但一页文字都没抽到（全是图片/截图）：不抛异常，靠 warning 兜住。"""
    result = extract_pptx(_build(blank=True))
    assert "no_extractable_text" in result.warnings
    assert result.pages == 1


def test_pages_counts_slides():
    from pptx import Presentation

    presentation = Presentation()
    for _ in range(3):
        presentation.slides.add_slide(presentation.slide_layouts[6])
    buffer = io.BytesIO()
    presentation.save(buffer)

    result = extract_pptx(buffer.getvalue())
    assert result.pages == 3
    assert result.backend == "python-pptx"


def test_invalid_pptx_raises_valueerror():
    """不是有效的 pptx（老 .ppt 改名、或损坏）抛 ValueError，让路由转 400。"""
    with pytest.raises(ValueError):
        extract_pptx(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 old OLE2 ppt")


# ========== 分派与白名单 ==========


def test_parse_document_dispatches_pptx():
    parsed = parse_document("方案汇报.pptx", _build())
    assert parsed.backend == "python-pptx"
    assert "差旅报销制度" in parsed.text


def test_pptx_is_in_the_document_whitelist():
    """pptx 进了 DOCUMENT，于是自动进知识库与附件白名单，也算"按文本读不出来"。"""
    assert "pptx" in file_types.DOCUMENT
    assert "pptx" in file_types.KNOWLEDGE
    assert "pptx" in file_types.ATTACHMENT
    assert "pptx" in file_types.BINARY_UNREADABLE
    # 它不再是"刻意排除"或旧二进制格式那一类
    assert "pptx" not in file_types.DELIBERATELY_EXCLUDED
    assert file_types.category_of("pptx") == "document"
