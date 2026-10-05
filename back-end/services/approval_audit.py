"""参数摘要：审批、审计与工单轨迹共用的同一套哈希口径。

与 ``approval.py`` 分开：那个模块管**判定**（参数改动是否合法、回灌给模型的说明
怎么写），这个模块只管**摘要**。两边都不碰数据库——留痕已经由 ``audit_log`` 与
``services/ticket/trace.py`` 各自负责，它们都调这里的函数。

## 摘要而不是完整参数

退款理由、地址、政策正文都可能很长。整份存进审计表等于同一份用户内容在库里存两遍，
而审计要回答的问题是"当时批准的到底是不是这一份"——SHA-256 足以证明同一性，预览
足以让人认出是哪一份。

摘要算在**序列化时按 key 排序**的 JSON 上，和 ``RepeatGuard.key`` 同一个理由：
``{"a":1,"b":2}`` 与 ``{"b":2,"a":1}`` 是同一份参数，不排序会让同一份参数算出
两个不同的 digest，于是"批准的和执行的是同一份"这个判断会假阴性。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 预览的截断长度。够认出"是哪一份"，不够复制一整篇文档
_PREVIEW_MAX_CHARS = 500


def digest(arguments: Any) -> str:
    """参数的 SHA-256。按 key 排序后序列化，见模块文档。"""
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        encoded = str(arguments)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def preview(arguments: Any) -> str:
    """给人看的参数预览，截断到 ``_PREVIEW_MAX_CHARS``。

    截断标记写出实际长度而不是只写省略号：审计时"这份参数原本有多长"本身
    就是信息——20000 字符的写入和 200 字符的写入是两件不同的事。
    """
    try:
        text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(arguments)
    if len(text) <= _PREVIEW_MAX_CHARS:
        return text
    return f"{text[:_PREVIEW_MAX_CHARS]}…（共 {len(text)} 字符）"


__all__ = ["digest", "preview"]
