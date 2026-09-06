"""人工审批闸门：哪些工具在执行前必须先问过人。

这和 ``workspace_tools._ToolApprovals``（确认令牌）是两层不同的东西，容易混：

- **确认令牌**扫的是用户原话里有没有"删除"这类字眼。它是**启发式**的，用来在
  没有审批 UI 时给破坏性操作一道门槛。词表判定必然有假阴假阳。
- **审批闸门**（这个模块）是**执行前真的停下来等一次点击**。它不猜用户的意思，
  它直接问。

两层同时留着不是冗余：审批关掉时确认令牌仍是唯一的门槛；审批开着时令牌变成
第二道锁（用户点了"同意"才把这一次调用的令牌打开，见 ``chat_service``）。

为什么闸门放在**工具执行的边界**而不是工具处理器内部：处理器返回的是一段
给模型看的文本，它没有"暂停整个回合"的能力——要暂停就得让处理器抛异常，
然后在循环里认那个异常，等于用异常做控制流。边界判定只需要一次字典查表。
"""
from __future__ import annotations

from typing import Any

from config import settings
from services.guardrails import mask_markup

# 默认需要审批的工具：能改变工作区状态的那些。
#
# 为什么不是"所有工具"：审批的成本是打断一个人。让检索也要点一下同意，
# 三次之后用户会开始无脑点确认——那时审批就只是仪式，不再是控制。
_DEFAULT_GATED = (
    "save_to_knowledge_base",
    "delete_knowledge_document",
    # 本机文件的写、改、删。爆炸半径比知识库那两个大一档：知识库文档是这个应用
    # 自己的数据，而这三个动的是用户机器上的真实文件——一次覆盖写没有回收站。
    #
    # 名字不从 fs_tools.WRITE_TOOLS 展开，是为了不让这个模块 import 那一边：
    # approval 被 chat_service 在工具执行边界上调用，而 fs_tools 持有数据库会话。
    # 两个字符串元组不一致的风险由 test_fs_tools 里一条断言钉住。
    "write_file",
    "edit_file",
    "delete_file",
)

# 每个受审工具在弹窗里的说明。讲清"批准之后会发生什么"，
# 而不是复述工具名——用户要判断的是后果，不是名字。
_REASONS = {
    "save_to_knowledge_base": (
        "将向工作区知识库写入一份新文档。写入后它会进入检索范围，"
        "之后每一轮 RAG 都可能引用它。"
    ),
    "delete_knowledge_document": (
        "将永久删除一份知识库文档，不可恢复。"
    ),
    # 这三条要把"动的是你机器上的真实文件"说出来。用户在这里判断的是后果，
    # 而"写文件"听起来和"写知识库"差不多——差别在于后者删错了还能重新上传。
    "write_file": (
        "将写入你本机的一个文件。若该文件已存在，原内容会被**整个覆盖**，"
        "不经过回收站，无法撤销。"
    ),
    "edit_file": (
        "将修改你本机的一个文件（替换其中一段文字）。改动直接落盘，无法撤销。"
    ),
    "delete_file": (
        "将永久删除你本机的一个文件。不进回收站，无法恢复。"
    ),
}

# 预览里每个参数值最多展示多少字符。写入操作的正文可能有几千字，
# 全塞进 SSE 事件既拖慢流也没人会读完
_PREVIEW_CHARS = 800


def enabled() -> bool:
    return settings.AGENT_APPROVAL_MODE != "off" and settings.AGENT_CHECKPOINT_ENABLED


def gated_tools() -> set[str]:
    """本次配置下需要审批的工具名。

    ``write``（默认）= 上面那两个破坏性写操作。
    ``listed`` = 完全由 ``AGENT_APPROVAL_TOOLS`` 决定，一个都不隐含。
    后者存在的意义是做对照实验：给 ``web_search`` 加审批能立刻看出
    "每一步都要点同意"的体验代价，而这件事讲道理是讲不清的。
    """
    if not enabled():
        return set()
    if settings.AGENT_APPROVAL_MODE == "listed":
        return {
            name.strip()
            for name in settings.AGENT_APPROVAL_TOOLS.split(",")
            if name.strip()
        }
    return set(_DEFAULT_GATED)


def requires_approval(tool_name: str) -> bool:
    return tool_name in gated_tools()


def reason_for(tool_name: str) -> str:
    return _REASONS.get(
        tool_name, "该操作会改变工作区状态，需要你确认后才执行。"
    )


def build_preview(
    arguments: dict[str, Any], extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """给审批界面看的参数预览。

    字符串值一律过 ``mask_markup`` 再截断。这些值是**模型写的**，而模型写的东西
    可能整段来自它刚抓的网页——审批弹窗原样渲染就是给注入内容多开一个展示位，
    而且这个位置的可信度比正文更高：用户正准备在这里点"同意"。

    ``extra`` 是**工具特有**的预览补充，由调用方算好传进来（目前只有文件操作的
    diff，见 ``fs_tools.preview_extra``）。为什么不在这个模块里按工具名分支：
    算 diff 要读磁盘上的当前文件，也就要拿到数据库会话与用户 id——而这个模块是在
    工具执行的边界上被调用的，让它持有那些东西就等于把边界判定和工具实现绑在一起。
    这里只负责合并，键名用 ``__`` 前缀与真实参数区分（前端按前缀分流渲染）。

    ``extra`` 里的值**不再过 mask_markup**：它们由产出方自己脱敏，因为只有产出方
    知道哪些部分是外部文本。这里再过一遍会把已经脱敏的标记二次转义。
    """
    preview: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str):
            masked = mask_markup(value)
            preview[key] = (
                masked[:_PREVIEW_CHARS] + "…"
                if len(masked) > _PREVIEW_CHARS
                else masked
            )
            if len(masked) > _PREVIEW_CHARS:
                preview[f"{key}__chars"] = len(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            preview[key] = value
        else:
            preview[key] = mask_markup(str(value))[:_PREVIEW_CHARS]
    if extra:
        preview.update(extra)
    return preview


def describe_mode() -> str:
    if not settings.AGENT_CHECKPOINT_ENABLED:
        return "off (checkpoint disabled)"
    if settings.AGENT_APPROVAL_MODE == "off":
        return "off"
    tools = ", ".join(sorted(gated_tools())) or "none"
    return f"{settings.AGENT_APPROVAL_MODE} ({tools})"


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

    **接受部分编辑**：``edited`` 只需要带用户真正改过的键。这一条原来是反的
    （缺键报"不能删除参数"），而那个要求和前端手上的东西正面冲突——
    弹窗拿到的是 ``build_preview`` 的产物，它对字符串做了两件有损的事：
    ``mask_markup`` 中和了标记语法，然后截断到 ``_PREVIEW_CHARS`` 并补一个 ``…``。
    要求回传全部键，等于要求客户端把它只有有损副本的那些值也发回来——
    一次"只改标题"的编辑会把两千字正文静默截成八百字的脱敏版。

    合并方向本来就保住了没回传的键（``{**original, **edited}``），所以放开缺键
    不会让任何东西失去保护：没给的键用原值，给了的键才覆盖。
    """
    if not isinstance(edited, dict):
        return None, "参数必须是一个对象。"
    extra = set(edited) - set(original)
    if extra:
        return None, f"不能新增参数：{'、'.join(sorted(extra))}。"
    return {**original, **edited}, ""


def edit_message(tool_name: str, changed: list[str], note: str = "") -> str:
    """用户改参数后放行时，回灌给模型的说明。

    必须让模型知道"执行了，但参数被人改过"，而不是让它以为自己那次调用原样跑了。
    否则它会照自己原来的参数向用户复述结果——用户刚把标题改掉，模型还在说旧标题。
    列出**改了哪些键**而不是新旧值全文：值可能是几千字的正文，塞进对话历史挤掉的是
    后面几轮的预算。
    """
    keys = "、".join(changed) if changed else "无"
    base = (
        f"操作已执行，但参数被用户修改过：{tool_name}。"
        f"被修改的参数：{keys}。"
        "向用户复述结果时请依据修改后的参数，不要引用你原来提议的值。"
    )
    if note.strip():
        return f"{base}\n用户补充说明：{mask_markup(note.strip())[:500]}"
    return base


def rejection_message(tool_name: str, note: str = "") -> str:
    """用户拒绝之后回灌给模型的工具结果。

    必须是"这次没执行、原因是用户不同意"，而不是一句通用失败：模型据此该做的是
    改方案或者问用户想怎么改，而不是换个参数把同一件事再试一遍。附上用户备注
    （如果有）——那通常正好是它需要的修改方向。
    """
    base = (
        f"操作已被用户拒绝：{tool_name} 没有执行。"
        "请不要重试这次操作。可以向用户说明你原本打算做什么，"
        "并询问是否需要调整方案。"
    )
    if note.strip():
        return f"{base}\n用户补充说明：{mask_markup(note.strip())[:500]}"
    return base


__all__ = [
    "build_preview",
    "describe_mode",
    "edit_message",
    "enabled",
    "gated_tools",
    "reason_for",
    "rejection_message",
    "requires_approval",
    "validate_edit",
]
