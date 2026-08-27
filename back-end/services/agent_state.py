"""一次回答的可序列化状态，以及中断请求的形状。

改动之前，一个回合的全部状态都活在 ``chat_service._run_turn`` 这个异步生成器的
局部变量里：``messages``、``round_index``、预算对象、重复计数、熔断计数、委派次数。
只要 SSE 连接断开，生成器就被垃圾回收，这些东西一起消失。于是三件事做不到：

1. **人类审批。** 写操作要等用户点一下"同意"，而那一下发生在**另一个 HTTP 请求**里。
2. **并行合并。** 两个子代理同时改预算和引用时，需要一份能被合并的状态，而不是
   闭包里的可变对象。
3. **重放。** 评估复现和事后调试要能"回到第 3 轮再跑一次"。

这个模块把那些局部变量提成一个扁平、JSON 可序列化的 ``TurnState``。

三个设计决定：

**只存数据，不存协作者。** ``ToolRuntime``、``ModelAdapter``、工具处理器闭包、
埋点 span 一个都不进快照——它们持有数据库会话与 HTTP 客户端，序列化没有意义。
恢复时按 ``TurnState`` 里的参数**重建**它们，所以重建路径必须是确定性的：
同样的 ``use_rag`` / ``workspace_id`` / 开关组合必须得到同一套工具面。这是
"恢复"与"重新开始"唯一的区别所在。

**恢复点落在工具执行的边界上，不落在流式输出中间。** 已经发给用户的文本收不回来，
从半句话中间恢复就是让用户看到重复内容。所以快照只在"模型说完、工具还没跑"
这个位置写，此时本轮还没有面向用户的输出。

**字段扁平且全部 JSON 原生。** 不用 pickle：pickle 的反序列化等于执行任意代码，
而这张表以后是要跨进程、跨版本读的。代价是嵌套结构（``messages``）得自己保证
里面没有非 JSON 值——视觉内容块是 dict/list/str，满足这一点。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

# 快照写在哪个位置。只有这几个点是安全的（见模块文档的第二条）
Phase = Literal[
    # 模型已给出 tool_calls，本轮工具一个都还没执行
    "pre_tools",
    # 卡在某个需要审批的工具之前，等用户裁决
    "waiting_approval",
    # 卡在 ask_user 之后，等用户回答那个澄清问题
    "waiting_input",
    # 本轮工具全部执行完，即将进入下一轮模型调用
    "post_tools",
]

# 运行状态。``waiting_approval`` / ``waiting_input`` 是"进程里没有它、但它还活着"
# 的两个状态——都靠快照跨过 HTTP 请求边界。
#
# 两者的区别在于**回灌什么**：审批回灌的是"这次执行了/没执行"，澄清回灌的是
# 用户写的那句话，它要以 ``role=tool`` 的身份接在模型那次 ask_user 调用后面。
# 走 role=user 的话模型看到的是"有人插了句话"，而不是"我问的问题有答案了"，
# 它会倾向于重新组织整个回答，而不是拿着答案接着做原来那件事。
RunStatus = Literal[
    "running",
    "waiting_approval",
    "waiting_input",
    "done",
    "failed",
    "abandoned",
]


@dataclass(slots=True)
class InterruptRequest:
    """一次中断：模型想做某件事，但要先问过人。

    ``preview`` 是给人看的，所以要过 ``mask_markup``——这段内容是模型写的，
    而模型写的东西可能来自它刚抓的网页。审批弹窗把它原样渲染，等于让注入
    内容多了一个展示位。
    """

    # ``tool_approval``：等一次裁决（同意/改了再同意/拒绝）。
    # ``user_input``：等一句回答（模型调了 ask_user）。
    #
    # 用同一个 dataclass 承载两种中断，是因为它们要的机制完全一样：落快照、
    # 跨 HTTP 请求、恢复时重建工具面并拨回余额。分成两个类会让 checkpoint_store
    # 和 resume 路径各写两遍。差别只在恢复时回灌什么，那是 resume 的事。
    kind: Literal["tool_approval", "user_input"]
    tool: str
    # 完整参数，审批界面据此展示"到底要写什么/删什么"；
    # ``user_input`` 时这里是 {"question": "..."}
    arguments: dict[str, Any]
    # 这次调用在本轮 pending_calls 里的下标。恢复时要从这里接着跑
    call_index: int
    tool_call_id: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "InterruptRequest":
        return cls(
            kind=raw.get("kind", "tool_approval"),
            tool=str(raw.get("tool") or ""),
            arguments=raw.get("arguments") or {},
            call_index=int(raw.get("call_index") or 0),
            tool_call_id=raw.get("tool_call_id"),
            reason=str(raw.get("reason") or ""),
        )


@dataclass(slots=True)
class TurnState:
    """一个回合的完整可恢复状态。

    字段分四组：**身份**（这是谁的哪一回合）、**参数**（重建工具面与生成配置所需）、
    **进度**（走到第几轮、messages 长什么样）、**约束余额**（预算、重复、熔断、委派）。

    最后一组是最容易被漏掉的。只存 ``messages`` 和 ``round_index`` 的话，恢复之后
    预算是满的、重复计数是零、熔断是空的——于是"中断一次"变成了"把所有上限重置
    一次"，一个被熔断的工具会在恢复后复活，用户点一次同意等于给模型多发六轮预算。
    """

    # ---- 身份 ----
    run_id: str
    chat_id: str
    user_id: str
    workspace_id: str
    is_admin: bool = False
    message_id: str | None = None
    # 主代理 run 为 None；子代理 run 指向它的父 run
    parent_run_id: str | None = None
    agent_role: str | None = None

    # ---- 重建工具面与生成所需的参数 ----
    # 这些必须存：恢复发生在另一个请求里，那边没有原始 ChatRequest
    use_rag: bool = False
    prompt: str = ""
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 2048
    top_p: float = 1.0
    prompt_ref: str = ""
    delegation_mode: str = "off"
    # 本回合的计划，``[{goal, tool}, ...]``。空列表 = 没开规划，或模型判断不用分步。
    #
    # 存进快照的理由和 messages 一样：恢复发生在另一个请求里，那边不会重新规划。
    # 少了它，用户点一次同意就等于把计划丢掉——而计划正文已经在 messages 里了，
    # 于是模型还能看到它，只有我们这边看不到。那种"半个状态"最难查。
    #
    # 不存 step 游标：没有可靠信号说明"这一步做完了"（一轮可能并行调三个工具，
    # 也可能一轮什么都没做完）。按轮次推游标是个看起来精确的假数字。
    # 计划到底有没有被照做，由 eval 侧用"点名的工具实际调没调"来量。
    plan: list[dict[str, Any]] = field(default_factory=list)

    # ---- 进度 ----
    messages: list[dict[str, Any]] = field(default_factory=list)
    round_index: int = 0
    phase: Phase = "pre_tools"
    status: RunStatus = "running"
    # 模型本轮请求的工具调用，序列化成 [{id, name, arguments}]。
    # 存原始 arguments 字符串而不是解析后的 dict：回灌给模型的 assistant 消息
    # 必须逐字一致，重新序列化一遍可能改变键序，模型对不上自己刚说过的话。
    pending_calls: list[dict[str, Any]] = field(default_factory=list)
    # 本轮已经执行完的调用数。恢复时从这个下标继续
    pending_index: int = 0
    # 已执行调用的结果，按顺序。恢复时用它把 messages 补齐（见 replay_writes）
    writes: list[dict[str, Any]] = field(default_factory=list)
    # 文本工具协议这一轮攒下的结果片段（GLM 那条路径不用 role=tool）
    text_results: list[str] = field(default_factory=list)
    uses_text_protocol: bool = False
    # 本轮已经有面向用户的文本流出去了。恢复时据此判断能不能重发
    streamed_text: bool = False
    # 中断之前已经发给用户的正文。少见但真实：模型可以先说一句"我来把这份
    # 整理好保存进知识库"再发起写调用，那句话已经在用户屏幕上了。
    # 恢复后落库时要把它接在前面，否则数据库里的回答比用户看到的少一句。
    streamed_prefix: str = ""
    emitted_any: bool = False
    force_final: bool = False

    # ---- 约束余额 ----
    budget_remaining: int = 0
    budget_per_call: int = 0
    repeat_counts: dict[str, int] = field(default_factory=dict)
    repeat_blocked: int = 0
    breaker_consecutive: dict[str, int] = field(default_factory=dict)
    breaker_tripped: list[str] = field(default_factory=list)
    delegations_used: int = 0

    # ---- 中断 ----
    interrupt: dict[str, Any] | None = None
    # 已获批准的 tool_call_id。恢复执行时凭它给这一次调用放行，
    # 而不是把整个回合的授权都打开
    approved_call_ids: list[str] = field(default_factory=list)
    rejected_call_ids: list[str] = field(default_factory=list)
    # 用户改过的参数，call_key -> JSON 字符串。
    #
    # 为什么必须存在这里，而不是就地改 messages 里那条 tool_calls：主循环每一轮
    # 都从 ``call.arguments`` 重新 ``json.loads``（见 chat_service 的闸门），所以
    # 改 dict 是留不住的——恢复时读到的还是模型原来那份。
    #
    # 为什么存字符串而不是 dict：``ToolRuntime.execute`` 校验的是
    # ``call.arguments`` 这个**字符串**。存字符串、执行前写回 ``call.arguments``，
    # 用户改过的参数就和模型写的走**同一条校验路径**。存 dict 再绕过校验塞进去，
    # 等于给客户端开了一条免检写入通道。
    edited_arguments: dict[str, str] = field(default_factory=dict)
    # ask_user 的回答，call_key -> 用户原话（已过 mask_markup）。
    # 和 edited_arguments 同一个思路：主循环每轮重跑，答案不能只活在闭包里。
    clarification_answers: dict[str, str] = field(default_factory=dict)
    # 用户拒绝时留下的话。会随拒绝结果回灌给模型——那通常正好是它需要的
    # 修改方向（"别写进知识库，先给我看看"）
    interrupt_note: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "TurnState":
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        # 只取认识的字段：快照可能是旧版本写的，多出来的键直接忽略，
        # 缺的键用默认值。这让"加字段"不需要迁移历史快照。
        return cls(**{key: value for key, value in data.items() if key in known})

    @property
    def interrupt_request(self) -> InterruptRequest | None:
        if not self.interrupt:
            return None
        return InterruptRequest.from_dict(self.interrupt)


def make_write(
    *,
    index: int,
    call_id: str | None,
    name: str,
    status: str,
    content: str,
    budget_after: int,
    repeat_counts: dict[str, int],
    repeat_blocked: int,
    breaker_consecutive: dict[str, int],
    breaker_tripped: list[str],
    citations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一次已完成工具调用的记录。

    ``content`` 存的是**已经过预算裁剪**的文本，也就是真正要回灌给模型的那一份。
    存裁剪前的原文会让恢复后的 messages 和中断前不一致——模型看到的东西变长了，
    而预算却已经按短的那份扣过。原文另有归宿（``message_tool_steps``）。

    守卫状态随每一步存一份快照，而不是恢复时按 writes 重新推算一遍：推算要求
    重复检测与熔断的规则永不改变，而它们恰恰是会调的（阈值、连续还是累计）。
    存快照的代价是每条记录多几十字节，换来的是"恢复后的余额和中断前逐位相同"
    这件事不依赖任何规则的稳定性。
    """
    return {
        "index": index,
        "call_id": call_id,
        "name": name,
        "status": status,
        "content": content,
        "citations": citations or [],
        "budget_after": budget_after,
        "guards": {
            "repeat_counts": dict(repeat_counts),
            "repeat_blocked": repeat_blocked,
            "breaker_consecutive": dict(breaker_consecutive),
            "breaker_tripped": list(breaker_tripped),
        },
    }


def replay_writes(state: TurnState) -> None:
    """把守卫余额与执行游标拨回中断那一刻。

    幂等性由两件事共同保证，这个函数负责第二件：

    1. **上下文里的结果已经在了。** 快照是在工具循环**进行中**拍的，而循环是
       直接往 ``state.messages`` 追加 ``role=tool`` 消息的（文本协议那条路径追加
       到 ``text_results``）。所以已完成调用的结果本来就在快照里——这里**绝不能**
       再按 ``writes`` 追加一遍，否则模型会看到同一个工具结果两次，
       而两次之间没有任何矛盾迹象，它只会当成"查了两轮都是这个答案"。
       （这一条是写完验证脚本之后才发现的：先前的实现确实追加了第二遍。）
    2. **游标与余额要接上。** ``pending_index`` 决定从第几个调用继续，
       余额决定它还能花多少。不拨的话，"中断一次"就等于"把所有上限重置一次"：
       用户点一下同意，模型又拿到一整份预算、清零的重复计数、复活的熔断工具。

    所以 ``writes`` 里的 ``content`` 不是给恢复用的（上下文里已经有了），
    它是审计与调试用的：出问题时要能回答"当时那一步到底把什么塞进了上下文"。
    """
    if not state.writes:
        return

    last = state.writes[-1]
    state.budget_remaining = int(last.get("budget_after", state.budget_remaining))
    guards = last.get("guards") or {}
    state.repeat_counts = dict(guards.get("repeat_counts") or {})
    state.repeat_blocked = int(guards.get("repeat_blocked") or 0)
    state.breaker_consecutive = dict(guards.get("breaker_consecutive") or {})
    state.breaker_tripped = list(guards.get("breaker_tripped") or [])
    state.pending_index = len(state.writes)


__all__ = [
    "InterruptRequest",
    "Phase",
    "RunStatus",
    "TurnState",
    "make_write",
    "replay_writes",
]
