"""工具结果的字符预算：扣费、退款、以及往轮结果的回收。

这两件事在 2026-09-05 一起改，因为它们互为前提：轮次上限从 6 提到 10 之后，
预算"只减不增"就从浪费变成卡死——多出来的四轮没有字符可花，等于白给。

另一半是一个真 bug：``take`` 在结果超出单次上限时把余额**归零**而不是扣掉实际
注入的字符。旧配置下很少撞到（知识库分块本来就短），打开文件工具之后
``search_files`` 一次轻易过 4000，于是一次搜索就把整回合预算清零。
"""
from __future__ import annotations

from services.chat_service import (
    _ToolResultBudget,
    _compact_stale_tool_results,
)


# ========== 扣费 ==========


def test_短结果按实际长度扣费():
    budget = _ToolResultBudget(total=100, per_call=50)
    assert budget.take("x" * 30) == "x" * 30
    assert budget.remaining == 70


def test_超出单次上限只扣实际注入的字符():
    """这是 2026-09-05 修掉的 bug。

    原来这里是 ``self._remaining = 0``：一个 9000 字的结果只注入 4000 字，
    却把总预算剩下的 7900 全部作废。后面几轮全部拿到"预算已用尽"，而工具轨迹里
    那次调用显示成功——"为什么它不接着读文件"没有任何线索。
    """
    budget = _ToolResultBudget(total=12000, per_call=4000)
    budget.take("x" * 100)
    out = budget.take("x" * 9000)

    assert out.startswith("x" * 4000)
    assert "已截断" in out
    assert budget.remaining == 12000 - 100 - 4000


def test_预算耗尽后不再注入():
    budget = _ToolResultBudget(total=10, per_call=10)
    budget.take("x" * 10)
    assert budget.exhausted
    assert "预算已用尽" in budget.take("y" * 5)


# ========== 退款 ==========


def test_退款不会让余额涨过初始总量():
    """退款的上限是"已经花掉的部分"。涨过初始总量就等于凭空发预算。"""
    budget = _ToolResultBudget(total=100, per_call=50)
    budget.take("x" * 10)
    assert budget.refund(9999) == 10
    assert budget.remaining == 100


def test_退款为零或负数是空操作():
    budget = _ToolResultBudget(total=100, per_call=50)
    budget.take("x" * 10)
    assert budget.refund(0) == 0
    assert budget.refund(-5) == 0
    assert budget.remaining == 90


# ========== 往轮结果回收 ==========


def _call(call_id: str, name: str) -> dict:
    """assistant 消息里 tool_calls 的形状（见 ModelCompletion.as_assistant_message）。

    工具名在这里，而不在 tool 消息里——tool 消息只有 tool_call_id。
    """
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _conversation() -> list[dict]:
    """三轮：列目录 → 搜索 → 读文件。最后一组（读文件）属于上一轮，要保留。"""
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "tool_calls": [_call("c1", "list_directory")]},
        {"role": "tool", "tool_call_id": "c1", "content": "L" * 2000},
        {"role": "assistant", "tool_calls": [_call("c2", "search_files")]},
        {"role": "tool", "tool_call_id": "c2", "content": "S" * 1500},
        {"role": "assistant", "tool_calls": [_call("c3", "read_file")]},
        {"role": "tool", "tool_call_id": "c3", "content": "R" * 3000},
    ]
    return messages


def _spend(budget: _ToolResultBudget, messages: list[dict]) -> None:
    for message in messages:
        if message.get("role") == "tool":
            budget.take(message["content"])


def test_导航类结果被压成一行并退款():
    messages = _conversation()
    budget = _ToolResultBudget(total=12000, per_call=4000)
    _spend(budget, messages)
    before = budget.remaining

    reclaimed = _compact_stale_tool_results(messages, budget)

    assert reclaimed > 3000
    assert budget.remaining == before + reclaimed
    assert "已从上下文移出" in messages[2]["content"]
    assert "已从上下文移出" in messages[4]["content"]


def test_读文件的结果不回收():
    """文件正文是模型推理的依据本身。压掉它等于让模型忘记自己读过什么，
    下一轮它只能再读一遍——那比不回收更贵。
    """
    messages = _conversation()
    budget = _ToolResultBudget(total=12000, per_call=4000)
    _spend(budget, messages)

    _compact_stale_tool_results(messages, budget)

    assert messages[6]["content"] == "R" * 3000


def test_最后一组结果一律保留():
    """模型刚看到的东西不能在它眼前消失——那会让"我上一轮查到了什么"变成一个
    它答不出的问题。

    这里把最后一组换成**可回收的工具**，所以它被保留的唯一理由就是"它是最后一组"。
    """
    messages = [
        {"role": "assistant", "tool_calls": [_call("c1", "list_directory")]},
        {"role": "tool", "tool_call_id": "c1", "content": "A" * 2000},
        {"role": "assistant", "tool_calls": [_call("c2", "search_files")]},
        {"role": "tool", "tool_call_id": "c2", "content": "B" * 2000},
    ]
    budget = _ToolResultBudget(total=12000, per_call=4000)
    _spend(budget, messages)

    _compact_stale_tool_results(messages, budget)

    assert "已从上下文移出" in messages[1]["content"]
    assert messages[3]["content"] == "B" * 2000


def test_重复调用不会二次退款():
    """幂等。压缩会真的改写 messages，而 messages 进快照——恢复之后这个函数会
    再跑一遍，那时已压过的消息必须按占位文本认出来。

    二次退款的后果是余额凭空变大，而模型实际付的 token 一点没少。
    """
    messages = _conversation()
    budget = _ToolResultBudget(total=12000, per_call=4000)
    _spend(budget, messages)

    first = _compact_stale_tool_results(messages, budget)
    after_first = budget.remaining
    second = _compact_stale_tool_results(messages, budget)

    assert first > 0
    assert second == 0
    assert budget.remaining == after_first


def test_比占位文本还短的结果不压():
    """压它只会变长。"""
    messages = [
        {"role": "assistant", "tool_calls": [_call("c1", "list_directory")]},
        {"role": "tool", "tool_call_id": "c1", "content": "空目录"},
        {"role": "assistant", "tool_calls": [_call("c2", "search_files")]},
        {"role": "tool", "tool_call_id": "c2", "content": "x"},
    ]
    budget = _ToolResultBudget(total=12000, per_call=4000)
    _spend(budget, messages)

    assert _compact_stale_tool_results(messages, budget) == 0
    assert messages[1]["content"] == "空目录"


def test_工具名从assistant消息取而不是writes():
    """这一条钉的是 2026-09-05 那个真 bug。

    第一版从 ``state.writes`` 建 ``call_id → name`` 映射，而 ``writes`` **每轮开头
    都被清空**（它只记本轮已完成的调用，为的是恢复幂等）。轮首压缩时它必然是空的，
    于是一条都压不掉——函数不报错，而单元测试里手工累积 writes 也测不出来。
    只有穿过 ``_drive_loop`` 的那条测试抓住了它。

    这里构造一份**只有 messages、没有任何 writes 概念**的输入：压缩必须照样发生。
    """
    messages = _conversation()
    budget = _ToolResultBudget(total=12000, per_call=4000)
    _spend(budget, messages)

    assert _compact_stale_tool_results(messages, budget) > 0


def test_没有tool_calls的对话是空操作():
    """纯问答（一轮都没调工具）时不该有任何动作。"""
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    budget = _ToolResultBudget(total=100, per_call=50)
    assert _compact_stale_tool_results(messages, budget) == 0


def test_知识库检索结果不回收():
    """检索到的分块是回答的**证据**，回答里还要标引用。压掉它模型就没法标了。"""
    messages = [
        {"role": "assistant", "tool_calls": [_call("c1", "search_knowledge_base")]},
        {"role": "tool", "tool_call_id": "c1", "content": "【参考 1】" + "K" * 2000},
        {"role": "assistant", "tool_calls": [_call("c2", "calculate")]},
        {"role": "tool", "tool_call_id": "c2", "content": "x" * 100},
    ]
    budget = _ToolResultBudget(total=12000, per_call=4000)
    _spend(budget, messages)

    assert _compact_stale_tool_results(messages, budget) == 0
    assert "【参考 1】" in messages[1]["content"]


# ========== 穿过 _drive_loop ==========


def test_回收真的接在循环里(db_real, monkeypatch):
    """上面那些都直接调 ``_compact_stale_tool_results``。这一条穿过循环。

    少了它，"函数完全正确、但 ``_drive_loop`` 里那行没接上"这种情况一条测试都
    不会红——而那正是整个回收对用户完全不存在的形状。

    造法：把总预算调到只够两次列目录，第三轮如果没有回收就必然拿到"预算已用尽"。
    """
    from config import settings
    from conftest import FakeKnowledgeService, ScriptedAdapter, collect, run
    from services import fs_roots
    from services.chat_service import ChatService

    import tempfile

    root = tempfile.mkdtemp()
    for index in range(30):
        with open(f"{root}/file{index}.txt", "w", encoding="utf-8") as handle:
            handle.write("x")
    fs_roots.add_root(db_real, "u1", root)

    monkeypatch.setattr(settings, "TOOL_FS_ENABLED", True)
    monkeypatch.setattr(settings, "RAG_PREFETCH", False)
    monkeypatch.setattr(settings, "AGENT_MAX_TOOL_ROUNDS", 6)
    # 一次列目录约 700 字符。给 1600 → 两次就见底，第三次要靠回收
    monkeypatch.setattr(settings, "TOOL_RESULT_TOTAL_CHARS", 1600)
    monkeypatch.setattr(settings, "TOOL_RESULT_MAX_CHARS", 800)
    # 重复检测会拦掉第二次同参调用，所以每轮换一个 path 写法
    monkeypatch.setattr(settings, "AGENT_REPEAT_LIMIT", 0)

    adapter = ScriptedAdapter(
        [
            {"tool_calls": [("list_directory", {"path": root})]},
            {"tool_calls": [("list_directory", {"path": f"{root}/."})]},
            {"tool_calls": [("list_directory", {"path": f"{root}/./."})]},
            {"text": "列完了"},
        ]
    )
    service = ChatService(model_adapter=adapter)
    service._knowledge_service = FakeKnowledgeService()

    events = run(
        collect(
            service.stream_ai_response(
                db_real, "u1", "c1", "列一下文件夹", use_rag=False
            )
        )
    )

    # 第三轮那次调用**拿到了真实结果**，而不是"预算已用尽"。
    # 没有回收的话前两轮就把 1600 花完了。
    tool_messages = [
        message
        for message in adapter.calls[-1]["messages"]
        if message["role"] == "tool"
    ]
    assert len(tool_messages) == 3
    assert not any("预算已用尽" in message["content"] for message in tool_messages)
    # 前两轮的结果已经被压成占位文本，最后一轮的保留
    assert "已从上下文移出" in tool_messages[0]["content"]
    assert "已从上下文移出" in tool_messages[1]["content"]
    assert "file0.txt" in tool_messages[2]["content"]
    assert events[-1]["content"] == "列完了"


# ========== 开关隔离这件事本身 ==========


def test_所有bool开关都被钉成代码默认值():
    """``conftest._pin_feature_flags`` 的不变量：任何 bool 型配置，在测试里读到的
    都是 ``config.py`` 里写的默认值，而不是开发机 ``.env`` 里的值。

    为什么这条测试值得存在：这个隔离机制在 conftest 里被改过四次，每次都是因为
    "新加的开关不在名单里"——最初逐个列，然后按 ``TOOL_/AGENT_/CLARIFY_`` 前缀列，
    2026-09-05 加 skill 时 ``SKILL_ENABLED`` 又漏了（新前缀），三条与 skill 毫无
    关系的测试变红。

    现在的判据是**类型**（bool 就是开关），不依赖命名。这条测试钉住它：以后有人
    改回前缀清单，或者加一个漏网的开关，这里立刻红——而不是等下一次某个无关测试
    莫名其妙地失败。

    注意它读的是 ``settings``，而 autouse 的 fixture 已经作用过了，所以这里看到的
    正是"测试环境里的实际取值"。
    """
    from conftest import _PINNED_FLAGS
    from config import Settings, settings

    # ``_PINNED_FLAGS`` 里显式列的那些是**刻意**偏离代码默认值的，放过它们。
    # 目前只有 USAGE_GUARD_ENABLED（代码默认 True，测试里必须关——同一个用户
    # 连发几十次请求必然撞上 20/min 的上限，而报错是 429 出现在被测功能里）。
    mismatched = [
        name
        for name, field in Settings.model_fields.items()
        if isinstance(field.default, bool)
        and name not in _PINNED_FLAGS
        and getattr(settings, name, field.default) != field.default
    ]
    assert not mismatched, (
        "这些 bool 开关在测试里没有被钉成 config.py 的默认值，"
        f"结果会取决于谁的 .env：{mismatched}"
    )
