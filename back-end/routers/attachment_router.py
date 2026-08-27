"""对话附件上传路由：把用户在聊天框上传的文件保存到本地，返回可访问的 URL。

支持三类，划分与白名单都来自 ``services.file_types``（单一真相源，别在这里
再列一份——那正是改动前的问题）：

- 文本类：前端会直接读取内容拼到 prompt，本接口仅做归档
- 图片类：前端通过 <img> 渲染，后端通过 /uploads 静态服务返回；额外做 magic bytes 校验
- 文档类（pdf）：走知识库解析链路，同样做签名校验
"""
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse

from auth import get_current_user
from config import settings
from services import file_types
from services.clock import now as app_now

router = APIRouter(prefix="/chats/attachments", tags=["对话附件"])

# 从 file_types 派生。svg / html 的排除**不在这里**表达了——它们不在任何基础类别里，
# 所以自动不在这个集合里。理由与那条安全测试的关系见 services/file_types.py
# （`DELIBERATELY_EXCLUDED`）。
ALLOWED_EXT = file_types.ATTACHMENT
MAX_SIZE = 10 * 1024 * 1024  # 10MB

# 图片 magic bytes 校验表
_IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": ("png",),
    b"\xff\xd8\xff": ("jpg", "jpeg"),
    b"GIF87a": ("gif",),
    b"GIF89a": ("gif",),
    b"RIFF": ("webp",),  # RIFF....WEBP
}

# PDF 签名
_PDF_SIGNATURE = b"%PDF"

# .docx 签名。OOXML 是个 ZIP 容器，所以这是 ZIP 的 local file header。
#
# 它挡不住"把 .xlsx 改名成 .docx"——两者签名相同，光看头四个字节区分不了。
# 那一层交给解析器：``ingest_clean.extract_docx`` 打不开就抛 ValueError，
# 路由转成 400 并给出"请确认是 .docx"的文案。
# 这里要挡的是另一件事，也是更常见的那件：**把纯文本或可执行文件改个扩展名传上来**。
# 那种内容连 ZIP 都不是，第一个字节就露馅。
_DOCX_SIGNATURE = b"PK\x03\x04"

# .xlsx 也是 OOXML/ZIP，签名与 docx 完全相同。
# 单独定义而不是共用一个 _OOXML_SIGNATURE：命名约定 _<EXT>_SIGNATURE 是
# test_document_category_has_a_signature_constant 的判据，而"两种格式恰好共享
# 同一个魔数"是实现细节,不该让下一个加格式的人去猜该复用哪个名字。
_XLSX_SIGNATURE = b"PK\x03\x04"

# 按扩展名查签名。文档类新增格式时改这一处，
# 而 test_document_category_has_a_signature_constant 盯着"有没有漏"。
_DOCUMENT_SIGNATURES = {
    "pdf": _PDF_SIGNATURE,
    "docx": _DOCX_SIGNATURE,
    "xlsx": _XLSX_SIGNATURE,
}


def _validate_image_magic_bytes(content: bytes, ext: str) -> bool:
    """校验图片文件头与扩展名是否一致"""
    for sig, exts in _IMAGE_SIGNATURES.items():
        if content.startswith(sig):
            if ext in exts:
                # webp 还需检查 RIFF 后面是否为 WEBP
                if ext == "webp" and content[8:12] != b"WEBP":
                    return False
                return True
    return False


@router.post("/upload")
async def upload_attachment(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")

    ext = ""
    if "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 .{ext}，允许：{', '.join(sorted(ALLOWED_EXT))}",
        )

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="文件超过 10MB 上限")

    # 图片类型做 magic bytes 校验。类别判定同样走 file_types：这里曾经是第七份
    # 副本（一个就地字面量的 image_exts），漏改它会让某种图片跳过伪造校验。
    is_image = file_types.category_of(ext) == "image"
    if is_image:
        if not _validate_image_magic_bytes(content, ext):
            raise HTTPException(
                status_code=400,
                detail="文件内容与扩展名不匹配,疑似伪造文件类型",
            )

    # 文档类做 magic bytes 校验。查表而不是逐个 if：
    # 漏掉一种格式时这里会静默放行，而白名单已经收了它。
    document_signature = _DOCUMENT_SIGNATURES.get(ext)
    if document_signature and not content.startswith(document_signature):
        raise HTTPException(
            status_code=400,
            detail="文件内容与扩展名不匹配,疑似伪造文件类型",
        )

    # 按年月分目录
    ym = app_now().strftime("%Y%m")
    upload_dir = os.path.join(settings.UPLOAD_DIR, ym)
    os.makedirs(upload_dir, exist_ok=True)

    # 文件名仅保留 uuid + 扩展名,去掉用户原始文件名中的特殊字符
    safe_name = f"{uuid.uuid4().hex}.{ext}"
    stored_path = os.path.join(upload_dir, safe_name)
    with open(stored_path, "wb") as f:
        f.write(content)

    # 静态访问 URL（main.py 会挂载 /uploads）
    url = f"/uploads/{ym}/{safe_name}"

    return JSONResponse({
        "url": url,
        "filename": file.filename,
        "size": len(content),
        "contentType": file.content_type or "application/octet-stream",
        "isImage": is_image,
    })
