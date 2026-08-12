"""对话附件上传路由：把用户在聊天框上传的文件保存到本地，返回可访问的 URL。

支持两类：
- 文本类（txt/md/代码/json/yaml...）：前端会直接读取内容拼到 prompt，本接口仅做归档
- 图片类（png/jpg/jpeg/gif/webp）：前端通过 <img> 渲染，后端通过 /uploads 静态服务返回
"""
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse

from auth import get_current_user
from config import settings

router = APIRouter(prefix="/chats/attachments", tags=["对话附件"])

# 允许的扩展名 (不含 svg/html 等可执行类型)
ALLOWED_EXT = {
    "txt", "md", "py", "js", "ts", "tsx", "jsx", "json", "xml", "yaml", "yml",
    "css", "csv", "log", "sh", "java", "go", "rs", "c", "cpp",
    "png", "jpg", "jpeg", "gif", "webp", "pdf",
}
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

    # 图片类型做 magic bytes 校验
    image_exts = {"png", "jpg", "jpeg", "gif", "webp"}
    is_image = ext in image_exts
    if is_image:
        if not _validate_image_magic_bytes(content, ext):
            raise HTTPException(
                status_code=400,
                detail="文件内容与扩展名不匹配,疑似伪造文件类型",
            )

    # PDF 做 magic bytes 校验
    if ext == "pdf":
        if not content.startswith(_PDF_SIGNATURE):
            raise HTTPException(
                status_code=400,
                detail="文件内容与扩展名不匹配,疑似伪造文件类型",
            )

    # 按年月分目录
    ym = datetime.now(timezone.utc).strftime("%Y%m")
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
