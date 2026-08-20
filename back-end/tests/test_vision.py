"""视觉通路的单元测试。

两条硬风险：非视觉模型收到内容块会拿到 400，所以白名单判断必须严格；
base64 把体积放大三分之一，所以张数与字节上限必须硬。另外文本替换只发生在
当前这一条用户消息上，历史消息不能有内容块（token 预算那边按纯字符串裁剪）。
"""
from __future__ import annotations

import pytest

from config import settings
from services import vision
from services.vision import build_user_content, _collect_references, _encode


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    target = tmp_path / "uploads"
    target.mkdir()
    (target / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    (target / "b.png").write_bytes(b"data" * 10)
    (target / "c.png").write_bytes(b"c" * 20)
    (target / "big.png").write_bytes(b"x" * 100)
    (target / "doc.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(target))
    monkeypatch.setattr(settings, "VISION_MAX_IMAGE_BYTES", 50)
    monkeypatch.setattr(settings, "VISION_MAX_IMAGES", 2)
    monkeypatch.setattr(settings, "VISION_MODELS", "glm-4v, glm-4.5v")
    return target


# ========== 引用收集 ==========


def test_markdown_image_is_collected(upload_dir):
    found = _collect_references("![图1](/uploads/a.png)")
    assert found == [("![图1](/uploads/a.png)", "图1", "/uploads/a.png")]


def test_bare_upload_path_is_collected(upload_dir):
    """裸路径不带显示名，label 回退成文件名。"""
    found = _collect_references("看 /uploads/a.png 这张")
    assert found == [("/uploads/a.png", "a.png", "/uploads/a.png")]


def test_markdown_image_is_not_double_matched(upload_dir):
    """挖空后再扫裸路径，否则 ![](...) 内部会再命中一次少前导斜杠的子串。"""
    assert len(_collect_references("![图](/uploads/a.png)")) == 1


def test_duplicate_images_are_deduped(upload_dir):
    """同一张图贴两次没必要传两份 base64，图像 token 直接翻倍。"""
    found = _collect_references("![a](/uploads/a.png) 然后 ![b](uploads/a.png)")
    assert len(found) == 1


def test_duplicate_across_forms_are_deduped(upload_dir):
    """/uploads/x 与 uploads/x 是同一张图，按原样比较会编码两遍。"""
    found = _collect_references("![a](/uploads/a.png) ![b](uploads/a.png)")
    assert len(found) == 1


def test_non_image_extension_is_skipped(upload_dir):
    assert _collect_references("![doc](/uploads/doc.pdf)") == []


# ========== 白名单与内容块 ==========


def test_unsupported_model_keeps_text_and_reports_skip(upload_dir):
    result = build_user_content("![图1](/uploads/a.png) 说明", model="glm-4.5-air")
    assert result.content == "![图1](/uploads/a.png) 说明"
    assert result.skipped == ["model_not_vision:glm-4.5-air"]
    assert not result.multimodal


def test_unsupported_when_model_empty(upload_dir):
    result = build_user_content("![图](/uploads/a.png)", model="")
    assert result.skipped == ["model_not_vision:"]


def test_vision_model_builds_image_blocks(upload_dir):
    result = build_user_content("![图1](/uploads/a.png) 说明", model="glm-4v")
    assert result.multimodal
    assert result.images == 1
    text_block, image_block = result.content
    assert text_block["type"] == "text"
    # 文本里的 URL 被换成序号标记：模型手上已有图像，再留一个它打不开的
    # 链接只会让它去"读链接"。
    assert "[图片 1：图1]" in text_block["text"]
    assert image_block["type"] == "image_url"
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")


def test_missing_file_reports_unresolved(upload_dir):
    result = build_user_content("![图](/uploads/nope.png)", model="glm-4v")
    assert not result.multimodal
    assert any(reason.startswith("unresolved:") for reason in result.skipped)


def test_too_large_image_is_skipped(upload_dir):
    result = build_user_content("![大](/uploads/big.png)", model="glm-4v")
    assert not result.multimodal
    assert result.skipped == ["too_large_or_unsupported"]


def test_image_limit_caps_blocks(upload_dir):
    """多图超上限时只取前 N 张，不能把请求体撑爆。"""
    text = "![a](/uploads/a.png) ![b](/uploads/b.png) ![c](/uploads/c.png)"
    result = build_user_content(text, model="glm-4v")
    assert result.images == 2
    assert "over_image_limit" in result.skipped
    assert "[图片 3" not in result.content[0]["text"]


def test_no_images_returns_plain_text(upload_dir):
    result = build_user_content("你好", model="glm-4v")
    assert result.content == "你好"
    assert result.images == 0


def test_empty_text(upload_dir):
    assert build_user_content("", model="glm-4v").content == ""


# ========== 编码 ==========


def test_encode_returns_data_uri_for_known_type(upload_dir):
    uri, size = _encode(os_join(upload_dir, "a.png"))
    assert uri.startswith("data:image/png;base64,")
    assert size > 0


def test_encode_rejects_unknown_type_and_oversize(upload_dir):
    assert _encode(os_join(upload_dir, "doc.pdf")) is None
    assert _encode(os_join(upload_dir, "big.png")) is None


def os_join(root, name):
    import os

    return os.path.join(str(root), name)