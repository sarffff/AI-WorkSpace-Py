"""工具轨迹的行为测试。

两类风险各占一半：**回灌太多**会把上下文预算吃光，让真正的问题被挤出去；
**回灌错了**更糟——把上一回合的结果摆成本轮的工具结果，模型会以为检索已经
做过而直接跳过。所以这里既断言压缩与预算，也断言措辞与边界。
"""
from __future__ import annotations

from typing import Any

from config import settings
from models import MessageToolStep
from services import tool_history
from services.clock import naive_now
from services.token_budget import get_token_counter


def _step(
    *,
    round_index: int = 1,
    call_index: int = 0,
    tool_name: str = "search_knowledge_base",
    status: str = "ok",
    arguments: dict[str, Any] | None = None,
    result_content: str | None = None,
    result_chars: int | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> MessageToolStep:
    """构造一条不入库的步骤。

    列默认值是 INSERT 时才生效的，直接实例化拿到的是 None，所以这里每个字段
    都显式给值——否则测的是"渲染 None"，不是渲染真实数据。
    """
    body = result_content or ""
    return MessageToolStep(
        id=f"s-{round_index}-{call_index}",
        chat_id="c1",
        message_id="m-user",
        round_index=round_index,
        call_index=call_index,
        tool_name=tool_name,
        tool_call_id=f"call-{call_index}",
        arguments=tool_history._dump(arguments),
        status=status,
        result_content=result_content,
        result_chars=result_chars if result_chars is not None else len(body),
        citations=tool_history._dump(citations) if citations else None,
        created_at=naive_now(),
    )


# ========== 回灌措辞 ==========


def test_empty_trajectory_yields_no_block():
    assert tool_history.render_block([], tools_available=True) == ("", 0)


def test_block_declares_itself_as_history_not_this_round():
    """这一条是整个模块最要紧的断言：一旦模型把它读成本轮的工具结果，
    就会认为检索已经做过，跳过这轮真正需要的那次调用。"""
    text, kept = tool_history.render_block([_step()], tools_available=True)

    assert kept == 1
    assert "此前" in text
    assert "不是本轮的工具结果" in text


def test_block_carries_tool_name_arguments_and_status():
    text, _kept = tool_history.render_block(
        [_step(arguments={"query": "试用期"})], tools_available=True
    )

    assert "search_knowledge_base" in text
    assert "query=试用期" in text
    assert "成功" in text


def test_prefetch_step_is_labelled_instead_of_round_zero():
    """预检索是回合开始前发生的，标成"第 0 轮"会让模型以为自己调过一次。"""
    text, _kept = tool_history.render_block(
        [_step(round_index=0)], tools_available=True
    )

    assert "预检索" in text
    assert "第 0 轮" not in text


def test_failed_steps_are_kept():
    """失败必须留在记录里。抹掉它等于让模型下个回合重走同一条死路。"""
    text, _kept = tool_history.render_block(
        [_step(status="unavailable"), _step(round_index=2, status="invalid_arguments")],
        tools_available=True,
    )

    assert "工具不可用" in text
    assert "参数错误" in text


def test_unknown_status_falls_back_to_raw_value():
    text, _kept = tool_history.render_block(
        [_step(status="weird")], tools_available=True
    )

    assert "weird" in text


def test_footer_tells_the_model_it_can_recall_tools():
    with_tools, _ = tool_history.render_block([_step()], tools_available=True)
    without_tools, _ = tool_history.render_block([_step()], tools_available=False)

    assert "重新调用" in with_tools
    # 本轮没有工具时不能叫它"重新调用"——那是在教模型去请求一个不存在的能力
    assert "重新调用" not in without_tools
    assert "没有可用工具" in without_tools


# ========== 摘要与截断 ==========


def test_summary_reports_original_length_when_truncated(monkeypatch):
    """摘要短于原文时必须说明原文多长。

    这不是装饰：模型据此知道"这里还有更多内容"，需要细节时会重新调工具，
    而不是拿一段摘要硬答。
    """
    monkeypatch.setattr(settings, "TOOL_HISTORY_STEP_CHARS", 20)
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 10_000)
    text, _kept = tool_history.render_block(
        [_step(result_content="内容" * 200)], tools_available=True
    )

    assert "原文 400 字" in text
    assert "…" in text


def test_short_result_is_not_labelled_as_summary(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_HISTORY_STEP_CHARS", 240)
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 10_000)
    text, _kept = tool_history.render_block(
        [_step(result_content="很短的结果")], tools_available=True
    )

    assert "结果：很短的结果" in text
    assert "原文" not in text


def test_condense_collapses_whitespace():
    """折行和缩进对模型没有信息量，却照样按 token 计价。"""
    assert tool_history._condense("第一行\n\n  第二行\t尾", 100) == "第一行 第二行 尾"


def test_condense_handles_empty_and_zero_limit():
    assert tool_history._condense("", 100) == ""
    assert tool_history._condense("内容", 0) == ""


def test_malformed_arguments_do_not_break_rendering():
    """轨迹是辅助信息，一条脏数据不该炸掉整段回灌。"""
    step = _step()
    step.arguments = "{不是合法 JSON"

    text, kept = tool_history.render_block([step], tools_available=True)

    assert kept == 1
    assert "search_knowledge_base" in text


def test_empty_arguments_render_as_bare_call():
    text, _kept = tool_history.render_block(
        [_step(tool_name="list_knowledge_documents", arguments={})],
        tools_available=True,
    )

    assert "list_knowledge_documents()" in text


# ========== 引用 ==========


def test_citations_carry_document_id(monkeypatch):
    """带 document_id 是为了让模型能直接接 read_document_chunk，
    否则它得先花一整轮 list_knowledge_documents 把 id 找回来。"""
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 10_000)
    text, _kept = tool_history.render_block(
        [
            _step(
                citations=[
                    {"document_id": "d1", "document_name": "notes.md", "chunk_index": 3}
                ]
            )
        ],
        tools_available=True,
    )

    assert "notes.md#3" in text
    assert "d1" in text


def test_citations_are_capped_and_the_rest_counted(monkeypatch):
    """引用全列出来的话，光 document_id 就能吃掉大半个预算。"""
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 10_000)
    citations = [
        {"document_id": f"d{index}", "document_name": f"doc{index}.md", "chunk_index": index}
        for index in range(5)
    ]

    text, _kept = tool_history.render_block(
        [_step(citations=citations)], tools_available=True
    )

    assert "doc0.md#0" in text
    assert "doc4.md#4" not in text
    assert "另有 2 处" in text


def test_citation_without_chunk_index_falls_back_to_name(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 10_000)
    text, _kept = tool_history.render_block(
        [_step(citations=[{"document_name": "notes.md"}])], tools_available=True
    )

    assert "notes.md" in text
    assert "notes.md#" not in text


def test_non_list_citations_are_ignored():
    step = _step()
    step.citations = '{"document_id": "d1"}'

    text, kept = tool_history.render_block([step], tools_available=True)

    assert kept == 1
    assert "引用" not in text


# ========== token 预算 ==========


def test_budget_drops_oldest_steps_first(monkeypatch):
    """超预算时丢最早的：追问几乎总是针对刚发生的那几步，
    而第一次"列一下有哪些文档"到第三个回合已经没人关心了。"""
    steps = [
        _step(round_index=index, arguments={"query": f"q{index}"})
        for index in range(1, 6)
    ]
    monkeypatch.setattr(settings, "TOOL_HISTORY_STEP_CHARS", 240)
    counter = get_token_counter("heuristic")
    per_step = counter.count(tool_history._render_step(steps[0], step_chars=240))
    overhead = counter.count(tool_history._BLOCK_HEADER) + counter.count(
        tool_history._FOOTER_WITH_TOOLS
    )
    # 预算刚好够两步，第三步必然放不下
    monkeypatch.setattr(
        settings, "TOOL_HISTORY_TOKEN_BUDGET", overhead + per_step * 2 + 1
    )

    text, kept = tool_history.render_block(steps, tools_available=True)

    assert kept == 2
    assert "q5" in text and "q4" in text
    assert "q1" not in text and "q3" not in text


def test_budget_too_small_for_the_header_yields_nothing(monkeypatch):
    """预算连标题都放不下时返回空，而不是一段只有标题没有内容的记录。"""
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 5)

    assert tool_history.render_block([_step()], tools_available=True) == ("", 0)


def test_zero_budget_yields_nothing(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 0)

    assert tool_history.render_block([_step()], tools_available=True) == ("", 0)


# ========== 落库与取回 ==========


def _record(db, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "chat_id": "c1",
        "message_id": "m-user",
        "round_index": 1,
        "call_index": 0,
        "tool_name": "search_knowledge_base",
        "status": "ok",
        "result": "检索结果",
    }
    payload.update(overrides)
    tool_history.record(db, **payload)


def test_record_and_load_roundtrip(db_real):
    _record(db_real, arguments={"query": "试用期"}, result="6 个月")

    steps = tool_history.load_recent(db_real, "c1")

    assert len(steps) == 1
    assert steps[0].tool_name == "search_knowledge_base"
    assert steps[0].result_content == "6 个月"
    assert steps[0].status == "ok"


def test_disabled_history_records_nothing(db_real, monkeypatch):
    """关掉即退回"每个回合从零开始"，这是对照实验要用的开关。"""
    monkeypatch.setattr(settings, "TOOL_HISTORY_ENABLED", False)
    _record(db_real)

    monkeypatch.setattr(settings, "TOOL_HISTORY_ENABLED", True)
    assert tool_history.load_recent(db_real, "c1") == []


def test_disabled_history_loads_nothing(db_real, monkeypatch):
    _record(db_real)
    monkeypatch.setattr(settings, "TOOL_HISTORY_ENABLED", False)

    assert tool_history.load_recent(db_real, "c1") == []


def test_store_truncates_but_keeps_the_original_length(db_real, monkeypatch):
    """存的是原文而不是摘要，但也不能无上限——一次检索结果能有几十 KB。"""
    monkeypatch.setattr(settings, "TOOL_HISTORY_STORE_MAX_CHARS", 10)
    _record(db_real, result="内容" * 100)

    step = tool_history.load_recent(db_real, "c1")[0]

    assert len(step.result_content) == 10
    # 截断前的长度必须留下，摘要里要靠它告诉模型原文还有多少
    assert step.result_chars == 200


def test_other_chat_never_leaks(db_real):
    _record(db_real, chat_id="c1")
    _record(db_real, chat_id="c2", result="别人的结果")

    steps = tool_history.load_recent(db_real, "c1")

    assert len(steps) == 1
    assert steps[0].result_content == "检索结果"


def test_current_turn_is_excluded_from_its_own_history(db_real):
    """重新生成同一条消息时，上一次的半截轨迹不能当成"以前做过的事"
    回灌给自己——模型会据此跳过本该重做的调用。"""
    _record(db_real, message_id="m-old", result="上一回合")
    _record(db_real, message_id="m-current", result="本回合")

    steps = tool_history.load_recent(db_real, "c1", exclude_message_id="m-current")

    assert [step.result_content for step in steps] == ["上一回合"]


def test_load_respects_fetch_limit(db_real, monkeypatch):
    for index in range(5):
        _record(db_real, round_index=index + 1, result=f"r{index}")
    monkeypatch.setattr(settings, "TOOL_HISTORY_FETCH_LIMIT", 2)

    steps = tool_history.load_recent(db_real, "c1")

    # 取回的是最近的两条，且按执行顺序（旧到新）返回
    assert [step.result_content for step in steps] == ["r3", "r4"]


def test_record_survives_a_broken_session():
    """落轨迹失败不该把回答弄挂——它是辅助能力，不是主链路。"""

    class BrokenSession:
        def add(self, _entity):
            raise RuntimeError("db down")

        def rollback(self):
            return None

    tool_history.record(
        BrokenSession(),
        chat_id="c1",
        message_id="m",
        round_index=1,
        call_index=0,
        tool_name="search_knowledge_base",
        status="ok",
        result="x",
    )


def test_load_survives_a_broken_session():
    class BrokenSession:
        def query(self, *_args, **_kwargs):
            raise RuntimeError("db down")

    assert tool_history.load_recent(BrokenSession(), "c1") == []


# ========== 失效清理 ==========


def test_discard_removes_only_the_named_turns(db_real):
    _record(db_real, message_id="m1", result="保留")
    _record(db_real, message_id="m2", result="丢弃")

    removed = tool_history.discard(db_real, "c1", ["m2"])
    db_real.commit()

    assert removed == 1
    assert [step.result_content for step in tool_history.load_recent(db_real, "c1")] == [
        "保留"
    ]


def test_discard_ignores_empty_input(db_real):
    _record(db_real)

    assert tool_history.discard(db_real, "c1", []) == 0
    assert tool_history.discard(db_real, "c1", [None, ""]) == 0
    assert len(tool_history.load_recent(db_real, "c1")) == 1


def test_discard_chat_clears_everything(db_real):
    _record(db_real, message_id="m1")
    _record(db_real, message_id="m2")

    assert tool_history.discard_chat(db_real, "c1") == 2
    db_real.commit()
    assert tool_history.load_recent(db_real, "c1") == []


# ========== 拼进请求 / 对外序列化 ==========


def test_build_messages_returns_a_single_system_message(db_real, monkeypatch):
    """用 system 而不是还原 role=tool：还原要连带伪造 assistant tool_calls 消息，
    缺一个 tool_call_id 就是 400，而 GLM 的文本工具协议根本没有 role=tool。"""
    monkeypatch.setattr(settings, "TOOL_HISTORY_TOKEN_BUDGET", 10_000)
    _record(db_real, arguments={"query": "试用期"}, result="6 个月")

    messages, kept = tool_history.build_messages(
        db_real, "c1", tools_available=True
    )

    assert kept == 1
    assert [message["role"] for message in messages] == ["system"]
    assert "search_knowledge_base" in messages[0]["content"]


def test_build_messages_is_empty_without_history(db_real):
    assert tool_history.build_messages(db_real, "c1", tools_available=True) == ([], 0)


def test_serialize_matches_the_sse_event_field_names(db_real, monkeypatch):
    """字段名与 tool_start / tool_result 对齐，前端两条路径才能共用渲染函数。"""
    monkeypatch.setattr(settings, "TOOL_HISTORY_STEP_CHARS", 240)
    _record(
        db_real,
        arguments={"query": "试用期"},
        result="6 个月",
        citations=[{"document_id": "d1", "document_name": "notes.md", "chunk_index": 3}],
    )

    payload = tool_history.serialize(tool_history.load_recent(db_real, "c1")[0])

    assert payload["tool"] == "search_knowledge_base"
    assert payload["status"] == "ok"
    assert payload["round"] == 1
    assert payload["input"] == {"query": "试用期"}
    assert payload["citations"][0]["document_name"] == "notes.md"
    assert payload["resultChars"] == 4
    assert payload["resultPreview"] == "6 个月"
    assert payload["createdAt"]


def test_serialize_tolerates_missing_optional_fields(db_real):
    _record(db_real, result="")

    payload = tool_history.serialize(tool_history.load_recent(db_real, "c1")[0])

    assert payload["input"] == {}
    assert payload["citations"] == []
    assert payload["resultPreview"] == ""
