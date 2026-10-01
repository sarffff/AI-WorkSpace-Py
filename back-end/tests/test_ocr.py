"""扫描件 OCR 兜底测试：闸门、逐页转写装配、半配置告警、create_document 接线。

渲染（pdfplumber→pypdfium2）与视觉模型两条真·外部腿都替身化：``_render_pdf_pages``
被 monkeypatch 成固定 PNG 列表，adapter 注入假对象。真渲染腿单独由
``test_render_pdf_pages_real`` 用 reportlab 生成的 PDF 验一次（CI 无 reportlab 时跳过），
这样编排逻辑是确定性的、不依赖模型，而"渲染到底能不能跑"也有一条真实证据。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from config import settings
from services import ocr
from conftest import run


class FakeAdapter:
    """按剧本逐页返回文本。元素是 Exception 时在该页抛出，验单页失败不拖垮整篇。"""

    def __init__(self, pages: list):
        self._pages = list(pages)
        self.calls: list[dict] = []

    async def complete(self, *, messages, tools, model, purpose, **kwargs):
        self.calls.append({"model": model, "purpose": purpose, "messages": messages})
        item = self._pages.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(content=item, tool_calls=[])


def _enable(monkeypatch, *, model="glm-4v", whitelist="glm-4v"):
    monkeypatch.setattr(settings, "INGEST_OCR_ENABLED", True)
    monkeypatch.setattr(settings, "INGEST_OCR_MODEL", model)
    monkeypatch.setattr(settings, "VISION_MODELS", whitelist)


# ========== maybe_ocr 的闸门 ==========


def test_maybe_ocr_disabled_returns_none():
    """默认关（_pin_feature_flags 把 bool 开关钉成代码默认 False）→ 不碰 OCR。"""
    assert run(ocr.maybe_ocr("pdf", b"%PDF-fake")) is None


def test_maybe_ocr_non_pdf_returns_none(monkeypatch):
    """本轮只做扫描件 PDF；图片型 docx/pptx 仍落 no_extractable_text。"""
    _enable(monkeypatch)
    assert run(ocr.maybe_ocr("docx", b"PK\x03\x04")) is None


def test_maybe_ocr_model_unset_warns(monkeypatch):
    """开了但没配模型：不静默返回 None，而是带 ocr_model_unset 的空结果（可查）。"""
    _enable(monkeypatch, model="")
    result = run(ocr.maybe_ocr("pdf", b"%PDF-fake"))
    assert result is not None and result.text == ""
    assert result.warnings == ["ocr_model_unset"]


def test_maybe_ocr_model_not_vision_warns(monkeypatch):
    """模型不在 VISION_MODELS 白名单：发图会 400，提前挡下并说清是哪个模型。"""
    _enable(monkeypatch, model="glm-4.5-air", whitelist="glm-4v")
    result = run(ocr.maybe_ocr("pdf", b"%PDF-fake"))
    assert result.warnings == ["ocr_model_not_vision:glm-4.5-air"]


def test_maybe_ocr_render_backend_missing_warns(monkeypatch):
    """渲染后端缺（pdfplumber/PIL）：同样是带具体 warning 的空结果，不是 None。"""
    _enable(monkeypatch)
    monkeypatch.setattr(ocr, "render_backend_available", lambda: False)
    result = run(ocr.maybe_ocr("pdf", b"%PDF-fake"))
    assert result.warnings == ["ocr_render_backend_missing"]


def test_maybe_ocr_runs_when_fully_configured(monkeypatch):
    """三道闸门全过：真的去渲染 + 逐页转写（这里渲染被替身成两页）。"""
    _enable(monkeypatch)
    monkeypatch.setattr(ocr, "render_backend_available", lambda: True)
    monkeypatch.setattr(ocr, "_render_pdf_pages", lambda *a, **k: ([b"p1", b"p2"], 2))
    adapter = FakeAdapter(["第一页正文足够长", "第二页正文足够长"])
    result = run(ocr.maybe_ocr("pdf", b"%PDF-fake", adapter=adapter))
    assert result.pages_ocred == 2
    assert "第一页正文足够长" in result.text and "第二页正文足够长" in result.text
    # 转写走的是视觉模型名 + purpose=ocr（埋点归因）
    assert adapter.calls[0]["model"] == "glm-4v"
    assert adapter.calls[0]["purpose"] == "ocr"


# ========== ocr_pdf 的逐页装配 ==========


def test_ocr_pdf_assembles_page_text(monkeypatch):
    monkeypatch.setattr(ocr, "_render_pdf_pages", lambda *a, **k: ([b"p1", b"p2"], 2))
    adapter = FakeAdapter(["页1内容足够长一些", "页2内容足够长一些"])
    result = run(ocr.ocr_pdf(b"x", adapter=adapter, model="glm-4v"))
    assert result.pages_total == 2 and result.pages_rendered == 2
    assert result.pages_ocred == 2
    assert result.text == "页1内容足够长一些\n\n页2内容足够长一些"
    assert result.warnings == []


def test_ocr_pdf_page_limit_warning(monkeypatch):
    """渲染只拿回前 N 页、总页数更大 → 入前 N 页 + ocr_page_limit 告警（非整篇失败）。"""
    monkeypatch.setattr(ocr, "_render_pdf_pages", lambda *a, **k: ([b"p1"], 5))
    adapter = FakeAdapter(["唯一一页的正文内容"])
    result = run(ocr.ocr_pdf(b"x", adapter=adapter, model="glm-4v"))
    assert result.pages_ocred == 1
    assert "ocr_page_limit:1/5" in result.warnings


def test_ocr_pdf_blank_and_short_pages_skipped(monkeypatch):
    """空白页（模型回"无文字"）与过短页都不计入正文，只留真内容。"""
    monkeypatch.setattr(ocr, "_render_pdf_pages", lambda *a, **k: ([b"p1", b"p2", b"p3"], 3))
    adapter = FakeAdapter(["无文字", "短", "这是真正的合同正文内容足够长"])
    result = run(ocr.ocr_pdf(b"x", adapter=adapter, model="glm-4v"))
    assert result.pages_ocred == 1
    assert result.text == "这是真正的合同正文内容足够长"


def test_ocr_pdf_page_failure_continues(monkeypatch):
    """单页转写抛异常：记一页的账，继续下一页，不让整篇作废。"""
    monkeypatch.setattr(ocr, "_render_pdf_pages", lambda *a, **k: ([b"p1", b"p2"], 2))
    adapter = FakeAdapter([RuntimeError("boom"), "第二页正文内容足够长"])
    result = run(ocr.ocr_pdf(b"x", adapter=adapter, model="glm-4v"))
    assert result.pages_ocred == 1
    assert result.text == "第二页正文内容足够长"
    assert any(w.startswith("ocr_page_failed:1:") for w in result.warnings)


def test_ocr_pdf_no_text_recovered(monkeypatch):
    """页都渲染出来了却一个字没抽到：text 空 + ocr_no_text_recovered（别和"没开"同形）。"""
    monkeypatch.setattr(ocr, "_render_pdf_pages", lambda *a, **k: ([b"p1"], 1))
    adapter = FakeAdapter(["无文字"])
    result = run(ocr.ocr_pdf(b"x", adapter=adapter, model="glm-4v"))
    assert result.text == ""
    assert "ocr_no_text_recovered" in result.warnings


def test_ocr_pdf_render_failure(monkeypatch):
    """渲染本身抛异常：text 空 + ocr_render_failed:<异常类型>，不往上抛。"""
    def _boom(*a, **k):
        raise ValueError("corrupt")

    monkeypatch.setattr(ocr, "_render_pdf_pages", _boom)
    result = run(ocr.ocr_pdf(b"x", adapter=FakeAdapter([]), model="glm-4v"))
    assert result.text == ""
    assert result.warnings == ["ocr_render_failed:ValueError"]


# ========== 真·渲染腿（pdfplumber→pypdfium2），单独验一次 ==========


def test_render_pdf_pages_real():
    """用 reportlab 造一个两页 PDF，真的渲染成 PNG，证明渲染腿不是空中楼阁。

    reportlab 不是运行时依赖（只测试用），CI 没装就跳过——渲染腿本身的依赖
    （pdfplumber/pypdfium2/Pillow）是声明过的，这里缺的只是"生成一个测试 PDF"。
    """
    reportlab = pytest.importorskip("reportlab")
    import io
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(300, 300))
    pdf.drawString(50, 150, "PAGE ONE")
    pdf.showPage()
    pdf.drawString(50, 150, "PAGE TWO")
    pdf.showPage()
    pdf.save()

    pages, total = ocr._render_pdf_pages(buf.getvalue(), dpi=100, max_pages=20)
    assert total == 2 and len(pages) == 2
    assert all(png[:8] == b"\x89PNG\r\n\x1a\n" for png in pages)


def test_render_pdf_pages_respects_max(monkeypatch):
    """封顶只渲染前 N 页，但总页数如实返回（上层据此判 ocr_page_limit）。"""
    reportlab = pytest.importorskip("reportlab")
    import io
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(200, 200))
    for _ in range(4):
        pdf.drawString(20, 100, "x")
        pdf.showPage()
    pdf.save()

    pages, total = ocr._render_pdf_pages(buf.getvalue(), dpi=72, max_pages=2)
    assert total == 4 and len(pages) == 2


# ========== _merge_ocr：把 OCR 结果并回解析结果 ==========


def test_merge_ocr_recovered_replaces_text_and_drops_trigger():
    from services.knowledge_service import ParsedDocument, _merge_ocr

    parsed = ParsedDocument(text="", backend="pdfplumber", warnings=["no_text_layer"])
    recovered = ocr.OcrResult(text="转写出来的正文", pages_ocred=3, warnings=["ocr_page_limit:3/9"])
    merged = _merge_ocr(parsed, recovered)
    assert merged.text == "转写出来的正文"
    assert merged.backend == "pdfplumber+ocr"
    assert "no_text_layer" not in merged.warnings
    assert "ocr_recovered:3" in merged.warnings
    assert "ocr_page_limit:3/9" in merged.warnings


def test_merge_ocr_empty_keeps_text_and_appends_warnings():
    from services.knowledge_service import ParsedDocument, _merge_ocr

    parsed = ParsedDocument(text="", backend="pdfplumber", warnings=["no_text_layer"])
    recovered = ocr.OcrResult(text="   ", warnings=["ocr_no_text_recovered"])
    merged = _merge_ocr(parsed, recovered)
    assert merged.text == "" and merged.backend == "pdfplumber"
    assert "no_text_layer" in merged.warnings  # 还得靠自检判 failed
    assert "ocr_no_text_recovered" in merged.warnings


# ========== create_document 接线：解析为空 → OCR 兜底 → 正常落库 ==========


def _no_text_parse(filename, content):
    from services.knowledge_service import ParsedDocument

    return ParsedDocument(text="", backend="pdfplumber", warnings=["no_text_layer"])


def test_create_document_ocr_recovers_scanned_pdf(db_real, monkeypatch):
    """扫描件 PDF 解析为空 → OCR 转写出正文 → 它参与内容哈希并落库,状态仍走后续索引。"""
    from services import knowledge_service
    from services.knowledge_service import KnowledgeService, _load_warnings

    monkeypatch.setattr(settings, "INGEST_OCR_ENABLED", True)
    monkeypatch.setattr(knowledge_service, "parse_document", _no_text_parse)

    async def fake_maybe_ocr(extension, content, *, adapter=None):
        assert extension == "pdf"  # 扩展名从 filename 正确提取
        return ocr.OcrResult(text="合同正文：赔付上限 500", pages_ocred=2, pages_total=2)

    monkeypatch.setattr(ocr, "maybe_ocr", fake_maybe_ocr)

    doc, duplicate = run(
        KnowledgeService().create_document(
            db_real, "扫描合同.pdf", b"%PDF-scanned", "w1",
            uploader_id="u1", visibility="workspace",
        )
    )
    assert duplicate is False
    assert doc.content == "合同正文：赔付上限 500"
    assert doc.parse_backend == "pdfplumber+ocr"
    warnings = _load_warnings(doc.parse_warnings)
    assert "no_text_layer" not in warnings
    assert "ocr_recovered:2" in warnings
    # 内容哈希按 OCR 正文算,不是按空串——否则两篇不同扫描件会撞成同一个哈希
    import hashlib
    assert doc.content_hash == hashlib.sha256("合同正文：赔付上限 500".encode()).hexdigest()


def test_create_document_ocr_disabled_leaves_empty(db_real, monkeypatch):
    """开关关着(默认)：解析为空就原样落库,no_text_layer 留着,交给 index 的自检判 failed。"""
    from services import knowledge_service
    from services.knowledge_service import KnowledgeService, _load_warnings

    # INGEST_OCR_ENABLED 由 _pin_feature_flags 钉成 False,这里不改
    monkeypatch.setattr(knowledge_service, "parse_document", _no_text_parse)
    called = False

    async def should_not_run(*a, **k):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(ocr, "maybe_ocr", should_not_run)

    doc, _ = run(
        KnowledgeService().create_document(
            db_real, "空扫描.pdf", b"%PDF", "w1", uploader_id="u1",
        )
    )
    assert called is False  # 关着时连 maybe_ocr 都不该进
    assert (doc.content or "") == ""
    assert "no_text_layer" in _load_warnings(doc.parse_warnings)
