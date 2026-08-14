"""把对话里的图片变成模型真正能看的内容。

现状是这样的：前端把图片上传到 ``/uploads/...``，然后往提示词里拼一段
``![名字](/uploads/202608/xxx.png)``。界面上图片好好地显示着，用户以为模型看见了，
而模型收到的只是一个 URL 字符串——它既打不开这个链接，也不会说自己看不见，
于是照着文件名编一段说明。这是整个项目里最容易骗到人的一处。

这里把那段 Markdown 引用换成 OpenAI 兼容的 ``image_url`` 内容块（data URI），
于是不改前端也能真的把图像传过去。

三个必要的闸门：

1. **模型白名单**（``VISION_MODELS``）。给非视觉模型发内容块只会换来一个 400。
   默认为空，也就是默认关闭——升级一次代码不该让所有请求突然改变形状。
2. **张数与体积上限**。图片按面积折算 token，一张高清图能顶几千字；而且
   base64 会把体积放大三分之一，直接决定请求体大小。
3. **只作用于当前这一条用户消息**。历史消息在 ``token_budget`` 那边是纯字符串，
   把内容块塞进历史会让预算裁剪和滚动摘要一起失效。
"""
from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from config import settings
from services.token_budget import message_text as _message_text
from services.workspace_tools import file_extension, resolve_upload_path

logger = logging.getLogger("vision")

_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# ![alt](/uploads/...) 以及裸的 /uploads/... 路径。前者是前端现在拼的形状，
# 后者是用户手输或模型复述时的形状，两种都认。
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*(\S+?)\s*\)")
_BARE_UPLOAD = re.compile(r"(?<![\w./-])(/?uploads/[\w./\-]+)")


@dataclass(slots=True)
class VisionResult:
    """替换后的文本、可直接塞进请求的内容块，以及被跳过的原因。

    ``skipped`` 会作为埋点属性留下来：用户抱怨"它没看见我的图"时，
    这里能直接回答是模型不支持、还是图太大、还是路径解析不到。
    """

    content: str | list[dict[str, Any]]
    images: int = 0
    skipped: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []

    @property
    def multimodal(self) -> bool:
        return isinstance(self.content, list)


def vision_models() -> set[str]:
    raw = settings.VISION_MODELS or ""
    return {name.strip() for name in raw.split(",") if name.strip()}


def supports_vision(model: str) -> bool:
    """模型是否在视觉白名单里。

    白名单而不是"猜名字里有没有 v"：模型命名毫无规律，猜错的代价是每个带图的
    请求都拿到 400，而用户看到的只是"发送失败"。
    """
    return bool(model) and model in vision_models()


def _encode(path: str) -> tuple[str, int] | None:
    """读成 data URI。超过体积上限或类型不认时返回 None。"""
    extension = file_extension(path)
    mime = _MIME_TYPES.get(extension)
    if mime is None:
        return None
    size = os.path.getsize(path)
    if size > settings.VISION_MAX_IMAGE_BYTES:
        return None
    with open(path, "rb") as handle:
        payload = handle.read()
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}", size


def _collect_references(text: str) -> list[tuple[str, str, str]]:
    """找出文本里所有图片引用，返回 (原始片段, 显示名, 路径)。

    保留出现顺序并去重：同一张图贴两次没必要传两份 base64，但顺序不能乱——
    「左边这张和右边这张有什么区别」全靠顺序才能对上。
    """
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def remember(raw: str, label: str, path: str) -> None:
        # 去重键去掉前导斜杠:``/uploads/x`` 与 ``uploads/x`` 是同一张图,
        # 按原样比较会把同一张编码两遍,图像 token 直接翻倍。
        key = path.lstrip("/")
        if file_extension(path) not in _MIME_TYPES or key in seen:
            return
        seen.add(key)
        found.append((raw, label or os.path.basename(path), path))

    # 先收 Markdown 引用,再把它们从待扫文本里挖空。不挖空的话下面的裸路径正则
    # 会在 ![](...) 内部再命中一次同一路径的子串(少一个前导斜杠),既绕过去重
    # 又会让后续的文本替换切坏这段 Markdown。
    scanned = text
    for match in _MARKDOWN_IMAGE.finditer(text):
        remember(match.group(0), match.group(1), match.group(2))
        scanned = scanned.replace(match.group(0), " " * len(match.group(0)), 1)

    for match in _BARE_UPLOAD.finditer(scanned):
        remember(match.group(1), "", match.group(1))
    return found


def build_user_content(text: str, *, model: str) -> VisionResult:
    """把用户消息变成内容块（有图且模型支持时），否则原样返回字符串。"""
    if not text:
        return VisionResult(content=text)

    references = _collect_references(text)
    if not references:
        return VisionResult(content=text)
    if not supports_vision(model):
        # 不改文本:让 URL 留在提示词里,模型至少还能说"你给了我一个图片链接"。
        # 悄悄删掉反而更糟——用户看到自己发了图,模型却完全不提这件事。
        return VisionResult(content=text, skipped=[f"model_not_vision:{model}"])

    remaining = max(1, settings.VISION_MAX_IMAGES)
    blocks: list[dict[str, Any]] = []
    skipped: list[str] = []
    body = text

    for index, (raw, label, path) in enumerate(references, start=1):
        if len(blocks) >= remaining:
            skipped.append("over_image_limit")
            break
        try:
            resolved = resolve_upload_path(path)
        except ValueError as exc:
            skipped.append(f"unresolved:{exc}")
            continue

        try:
            encoded = _encode(resolved)
        except OSError as exc:
            skipped.append(f"unreadable:{type(exc).__name__}")
            continue
        if encoded is None:
            skipped.append("too_large_or_unsupported")
            continue

        data_uri, _size = encoded
        blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
        # 文本里把 URL 换成序号标记:模型手上已经有图像了,再留一个它打不开的
        # 链接只会让它去"读链接"。序号则让文字能指认具体哪一张。
        body = body.replace(raw, f"[图片 {index}：{label}]")

    if not blocks:
        return VisionResult(content=text, skipped=skipped)

    content: list[dict[str, Any]] = [{"type": "text", "text": body}]
    content.extend(blocks)
    return VisionResult(content=content, images=len(blocks), skipped=skipped)


def message_text(content: Any) -> str:
    """转发到 ``token_budget.message_text``。

    实现放在 token_budget 而不是这里:``model_adapter`` 的本地估算需要它,
    而它已经依赖 token_budget;如果反过来去 import vision,就会绕出一条
    model_adapter → vision → workspace_tools → knowledge_service 的环。
    """
    return _message_text(content)



