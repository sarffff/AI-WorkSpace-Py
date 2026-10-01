"""扫描件 OCR 兜底：没有文本层的 PDF，用视觉模型把每页转写成文字再入库。

``index_document`` 的 ``no_chunks`` 自检能把"扫描件抽不到字"从**静默**失败变成一条
``failed``，但它兜不住的是"让这篇文档真的进库"。扫描件 PDF（只有图像层、没有文本
层）在 ``ingest_clean.extract_pdf`` 里落 ``no_text_layer``、正文为空——而企业知识库里
合同、证照、历史存档恰恰大量是扫描件，RAG 的价值上限卡在这里。

**为什么是视觉模型而不是 tesseract**：项目已经说 OpenAI 兼容的多模态（见
``services/vision.py``），接视觉模型不引入外部二进制；tesseract 要装系统包 + 语言包，
中文竖排/表格/手写它都弱。渲染后端用 pdfplumber(0.11+) 的 ``to_image``，它走
pypdfium2——同样没有外部二进制（ImageMagick/Wand 都不需要）。

三道闸门（与 vision.py 同构，都在 ``maybe_ocr`` 里）：
1. 开关 ``INGEST_OCR_ENABLED``，默认关。打开一个入口就是把它的失败模式和成本一起打开。
2. 模型 ``INGEST_OCR_MODEL`` 必须显式配，且必须在 ``VISION_MODELS`` 白名单里——给非
   视觉模型发图只换来一个 400。
3. 页数上限 ``INGEST_OCR_MAX_PAGES``：扫描件动辄上百页，不封顶一次上传就能打爆配额。

半配置（开了但没配模型 / 模型不在白名单 / 渲染后端缺）一律返回带**具体 warning**
的空结果，绝不静默退回"没 OCR"——否则"扫描件为什么还是没进库"会变成查不出的问题。
"""
from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass, field

from config import settings

logger = logging.getLogger("ocr")

# 触发 OCR 的解析告警。这两个是 ingest_clean 对"有文件、一个字都没抽到"的两种叫法
# （PDF 用 no_text_layer，OOXML 用 no_extractable_text），语义相同。
OCR_TRIGGER_WARNINGS = frozenset({"no_text_layer", "no_extractable_text"})

# 转写指令。固定、无占位符，所以留在这里而不进 prompt_library（那套占位符契约校验是
# 为带变量、要 A/B 的回答提示词准备的，OCR 指令两者都不是）。要点：逐字转写、保留
# 结构、不要解释/翻译/补全——OCR 的产物要当正文入库再被检索，任何"模型自己的话"
# 混进去都会污染召回。
_OCR_INSTRUCTION = (
    "你是 OCR 引擎。逐字转写这一页图片里的所有文字，按阅读顺序输出。"
    "标题用 Markdown 的 # 层级表示，表格尽量转成 Markdown 表格。"
    "只输出页面上真实存在的文字：不要翻译、不要解释、不要补全、不要添加任何说明。"
    "如果这一页没有任何文字（纯插图或空白），只输出两个字：无文字。"
)
# 模型对空白页的约定回答。命中则这一页记空，不混进正文。
_BLANK_PAGE_MARKER = "无文字"


@dataclass(slots=True)
class OcrResult:
    """OCR 产出。``warnings`` 与 ingest 其它环节同一套词汇，一起落进 parse_warnings。"""

    text: str = ""
    pages_total: int = 0       # 文档总页数
    pages_rendered: int = 0    # 实际送去转写的页数（受 max_pages 限）
    pages_ocred: int = 0       # 真的抽到文字的页数
    warnings: list[str] = field(default_factory=list)


def render_backend_available() -> bool:
    """渲染后端装上了吗。pdfplumber(0.11+) 的 to_image 走 pypdfium2，无外部二进制。

    和 ``ingest_clean.structure_backend_available`` 一个道理：缺依赖不该静默退回
    "没 OCR"，而要能在启动时（见 main._check_ingest_backend）查出来。
    """
    try:
        import pdfplumber  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _render_pdf_pages(content: bytes, *, dpi: int, max_pages: int) -> tuple[list[bytes], int]:
    """把 PDF 的前 max_pages 页渲染成 PNG 字节。返回 (png_list, 总页数)。

    渲染是同步、CPU 密集的，但对扫描件的页数（几页到几十页）足够快，没必要丢进
    线程池徒增复杂度。真正慢的是下面每页一次的视觉模型调用，那是 await 的。
    """
    import pdfplumber

    pages: list[bytes] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        total = len(pdf.pages)
        for page in pdf.pages[: max(1, max_pages)]:
            image = page.to_image(resolution=dpi)
            buffer = io.BytesIO()
            image.original.save(buffer, format="PNG")
            pages.append(buffer.getvalue())
    return pages, total


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def _ocr_page(adapter, model: str, png: bytes) -> str:
    """转写一页。调用方负责把异常收敛成"这页没抽到"，这里只管发请求取文本。"""
    content = [
        {"type": "text", "text": _OCR_INSTRUCTION},
        {"type": "image_url", "image_url": {"url": _data_uri(png)}},
    ]
    completion = await adapter.complete(
        messages=[{"role": "user", "content": content}],
        tools=[],
        model=model,
        # 转写不是创作：temperature 0 既稳，也让同一份扫描件两次上传更可能得到同样
        # 的文本——create_document 的内容哈希去重依赖它（见下方 maybe_ocr 的调用点）。
        temperature=0.0,
        max_tokens=4096,
        purpose="ocr",  # 埋点用途分类，保证这几次调用的 token 用量被记进 trace
    )
    return (completion.content or "").strip()


async def ocr_pdf(
    content: bytes,
    *,
    adapter=None,
    model: str | None = None,
    dpi: int | None = None,
    max_pages: int | None = None,
) -> OcrResult:
    """渲染 + 逐页转写一个扫描件 PDF。只在 maybe_ocr 判过闸门后调用。"""
    model = model or settings.INGEST_OCR_MODEL
    dpi = dpi or settings.INGEST_OCR_DPI
    max_pages = max_pages or settings.INGEST_OCR_MAX_PAGES
    if adapter is None:
        # 懒 import + 懒构造，与 retriever._get_model_adapter 同一套路：避免
        # ocr -> model_adapter 的 import 期依赖，也让测试能注入假 adapter。
        from services.model_adapter import OpenAICompatibleAdapter

        adapter = OpenAICompatibleAdapter()

    try:
        pages, total = _render_pdf_pages(content, dpi=dpi, max_pages=max_pages)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR 渲染失败: %s", type(exc).__name__)
        return OcrResult(warnings=[f"ocr_render_failed:{type(exc).__name__}"])

    result = OcrResult(pages_total=total, pages_rendered=len(pages))
    if total > len(pages):
        # 入库的是前 N 页，不是整篇失败——半篇可检索的扫描件好过一篇 failed。
        result.warnings.append(f"ocr_page_limit:{len(pages)}/{total}")

    blocks: list[str] = []
    for index, png in enumerate(pages, start=1):
        try:
            text = await _ocr_page(adapter, model, png)
        except Exception as exc:  # noqa: BLE001
            # 单页失败不该让整篇作废：记一页的账，继续下一页。
            logger.warning("OCR 第 %d 页失败: %s", index, type(exc).__name__)
            result.warnings.append(f"ocr_page_failed:{index}:{type(exc).__name__}")
            continue
        if len(text) < settings.INGEST_OCR_MIN_CHARS or text == _BLANK_PAGE_MARKER:
            continue
        result.pages_ocred += 1
        blocks.append(text)

    result.text = "\n\n".join(blocks)
    if not blocks and not any(w.startswith("ocr_render_failed") for w in result.warnings):
        # 页都渲染出来了却一个字没抽到：可能真是空文档，也可能模型/配置有问题。
        # 记下来，别让它和"没开 OCR"长成一个样子。
        result.warnings.append("ocr_no_text_recovered")
    return result


async def maybe_ocr(extension: str, content: bytes, *, adapter=None) -> OcrResult | None:
    """入库路径的 OCR 入口。返回 None = 不该跑 OCR（上层保持原 warning 不动）。

    返回 ``OcrResult``（哪怕 text 为空）= 跑了、或该跑但跑不动，上层据此改写 warning。
    半配置一律返回带具体 warning 的空结果，绝不静默返回 None。
    """
    if not settings.INGEST_OCR_ENABLED:
        return None
    if extension != "pdf":
        # 图片型 docx/pptx 的内嵌图抽取是另一件事（质量存疑、常是 logo 而非内容页），
        # 本轮只做扫描件 PDF。那两类仍落 no_extractable_text。
        return None

    from services import vision

    model = settings.INGEST_OCR_MODEL
    if not model:
        return OcrResult(warnings=["ocr_model_unset"])
    if not vision.supports_vision(model):
        # 必须在 VISION_MODELS 白名单里，复用同一张白名单：给非视觉模型发图是 400。
        return OcrResult(warnings=[f"ocr_model_not_vision:{model}"])
    if not render_backend_available():
        return OcrResult(warnings=["ocr_render_backend_missing"])

    return await ocr_pdf(content, adapter=adapter, model=model)
