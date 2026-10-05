"""审批时"看到的参数"与"真正执行的参数"必须是同一份。

工单域的资金类操作会挂起等人裁决，而人可以在弹窗里**改参数**再批准。这里只判
一件事：改过的版本还是不是用户批准的那个形状。至于哪些工具要审批、审批文案怎么写，
住在 ``services/ticket/tools.py``（权限分级）与 ``services/ticket/graph.py``（人在回路
节点）里——那两处才有工单上下文，而这个函数是纯的。
"""
from __future__ import annotations

from typing import Any


def validate_edit(
    original: dict[str, Any], edited: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """校验用户改过的参数，返回 ``(可用参数, 错误说明)``。

    只允许**改已有键的值**，不允许增键。理由是这一层没有工具 schema：
    真正的 schema 校验在 ``ToolRuntime._validate``（执行前一定会走），这里挡的是
    另一类东西——凭空多出来的键说明客户端在拼一个模型从没提议过的调用形状，
    而用户在弹窗里看到并同意的是**模型那次调用**。

    这不是防御恶意客户端的最后一道门（那道门是 schema 校验 + 工具自身的权限
    检查），是让"越权改写"和"手滑打错字"在报错信息上分得开。

    **接受部分编辑**：``edited`` 只需要带用户真正改过的键。合并方向本来就保住了
    没回传的键（``{**original, **edited}``），所以放开缺键不会让任何东西失去保护：
    没给的键用原值，给了的键才覆盖。反过来要求回传全部键，等于要求客户端把它
    只有有损副本（弹窗里的预览截断过、脱敏过）的那些值也发回来。
    """
    if not isinstance(edited, dict):
        return None, "参数必须是一个对象。"
    extra = set(edited) - set(original)
    if extra:
        return None, f"不能新增参数：{'、'.join(sorted(extra))}。"
    return {**original, **edited}, ""


__all__ = ["validate_edit"]
