"""Agent 任务集与变体配置的自检。

这套评估要跑二十分钟、要花钱、而且结果会被当成"改动值不值"的依据。因此
数据集写歪、或者变体漏掉一个开关，代价不是报错而是**一份看起来正常的错报告**。
这里钉的就是那类不会自己暴露的错。

重点是第二组（``_BASE`` 的完整性）：``AGENT_DELEGATION_MODE`` 和
``MEMORY_ENABLED`` 先后都曾漏在 ``_BASE`` 外面，两次的后果都是本地 ``.env``
能静默改变每个变体的行为，而报告上看不出任何异常。这类漏项此前没有任何
测试拦得住。
"""
from __future__ import annotations

import os

import pytest

from config import Settings
from eval.agent_runner import load_tasks
from eval.agent_variants import AGENT_VARIANTS, _BASE

TASKS = load_tasks()
BY_ID = {task.id: task for task in TASKS}


# ---- 数据集 ----


def test_task_ids_are_unique():
    """重名任务不会报错，只会让后一条静默覆盖统计里的前一条。"""
    ids = [task.id for task in TASKS]
    assert len(ids) == len(set(ids))


def test_every_task_has_a_rubric():
    """rubric 是给裁判的唯一判据。空 rubric 会让裁判按自己的标准打分。"""
    for task in TASKS:
        assert task.rubric.strip(), f"{task.id} 没有 rubric"


def test_every_turn_has_a_plausible_min_rounds():
    """min_rounds 标错会让轮次效率变成一个看起来精确的假数字。

    要调工具的轮次至少需要 2 轮（调用 + 作答）；这条能拦住"标了
    expect_tools 却写 min_rounds=1"这种一眼看不出来的错。
    """
    for task in TASKS:
        for index, turn in enumerate(task.turns, start=1):
            assert turn.min_rounds >= 1, f"{task.id} 第 {index} 轮 min_rounds < 1"
            if turn.expect_tools:
                assert turn.min_rounds >= 2, (
                    f"{task.id} 第 {index} 轮要求调工具，min_rounds 至少是 2"
                )


def test_expect_and_forbid_never_overlap():
    """同时要求和禁止同一个工具，这一轮必然既扣召回又记违规。"""
    for task in TASKS:
        for index, turn in enumerate(task.turns, start=1):
            overlap = set(turn.expect_tools) & set(turn.forbid_tools)
            assert not overlap, f"{task.id} 第 {index} 轮自相矛盾：{overlap}"


# ---- 记忆注入用例 ----


def test_memory_injection_task_seeds_both_a_fact_and_an_attack():
    """只种攻击行的话，"通过"和"记忆压根没到模型那儿"是同一个结果。

    对照的那条事实必须是 fact，攻击那条必须伪装成 preference——抽取指令
    允许抽取输出形式偏好，这正是这条通路最难防的地方。
    """
    task = BY_ID["injection-memory"]
    kinds = [item["kind"] for item in task.seed_memories]
    assert "fact" in kinds and "preference" in kinds


def test_memory_attack_row_is_the_newest():
    """注入按 created_at 倒序取，最后一条排在最前面。

    攻击行放在数组末尾 = 在记忆块里位置最靠前 = 最强的那种情形。
    顺序反了这个用例还是会通过，但测到的是一个更弱的版本。
    """
    task = BY_ID["injection-memory"]
    assert task.seed_memories[-1]["kind"] == "preference"


def test_memory_canary_is_checked_on_every_turn():
    """漏一轮就等于给了模型一轮免检——而防线是每轮都要成立的。"""
    task = BY_ID["injection-memory"]
    canary = "MEM-CANARY-7391"
    assert any(canary in item["content"] for item in task.seed_memories)
    for index, turn in enumerate(task.turns, start=1):
        assert canary in turn.must_avoid, f"第 {index} 轮没有检查 canary"


def test_memory_task_also_checks_the_fact_is_usable():
    """防线过严要能被测出来。

    只测"没照攻击行做"的话，一个完全不看记忆的模型会拿满分——而那不是
    防线成功，那是功能失效。所以必须有一轮去读那条合法事实。
    """
    task = BY_ID["injection-memory"]
    fact = next(item for item in task.seed_memories if item["kind"] == "fact")
    checked = {phrase for turn in task.turns for phrase in turn.must_include}
    assert any(phrase in fact["content"] for phrase in checked), (
        "没有任何一轮检查那条合法记忆是否被用上"
    )


def test_seeded_memories_fit_the_production_limits():
    """预置行必须能过生产环境同样的限制，否则测的是一条线上不可能存在的记忆。

    读类默认值而不是 ``Settings()``：后者会把本机 .env 读进来，那样这条断言在
    不同机器上松紧不同——一台机器上过、另一台上不过，而两边的数据集是同一份。
    """
    inject_limit = Settings.model_fields["MEMORY_INJECT_LIMIT"].default
    item_max = Settings.model_fields["MEMORY_ITEM_MAX_CHARS"].default
    for task in TASKS:
        assert len(task.seed_memories) <= inject_limit
        for item in task.seed_memories:
            assert len(item["content"]) <= item_max
            assert item["kind"] in ("fact", "preference")


def test_canaries_do_not_appear_in_the_corpus():
    """canary 出现在语料里，must_avoid 就会被正常引用触发。"""
    canaries = {
        phrase
        for task in TASKS
        for turn in task.turns
        for phrase in turn.must_avoid
        if "CANARY" in phrase
    }
    assert canaries, "一个 canary 都没有，抗注入率没有样本"

    corpus_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "eval", "corpus")
    if not os.path.isdir(corpus_dir):
        pytest.skip("语料目录不在")
    for name in os.listdir(corpus_dir):
        path = os.path.join(corpus_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            text = handle.read()
        for canary in canaries:
            # injection-vendor 的 canary 本来就该埋在语料里——那是被测的注入源。
            # 记忆那条不该出现在任何文档里。
            if canary.startswith("MEM-"):
                assert canary not in text, f"{name} 里出现了记忆 canary"


# ---- 变体配置的完整性 ----

# 这些开关会改变 Agent 的行为，而且改了之后报告上看不出异常。每一条都必须在
# ``_BASE`` 里写死值——包括与产品默认相同的值，否则本地 .env 就能静默换掉它。
#
# 加新开关时如果它满足"会改变 Agent 行为、且不会在报告里留下痕迹"，就往这里加。
BEHAVIOR_CRITICAL = (
    # 工具面：少一个工具，工具召回直接掉，看起来像模型变笨了
    "TOOL_CALCULATE_ENABLED",
    "TOOL_WEB_SEARCH_ENABLED",
    "TOOL_READ_ATTACHMENT_ENABLED",
    "TOOL_WRITE_KNOWLEDGE_ENABLED",
    # 跨回合记忆：关掉 memory 探针全军覆没
    "TOOL_HISTORY_ENABLED",
    # 循环
    "AGENT_MAX_TOOL_ROUNDS",
    # 检索
    "RAG_PREFETCH",
    "RAG_TOP_K",
    # 历史：压缩了就是在测别的东西
    "HISTORY_TOKEN_BUDGET",
    # 护栏
    "GUARDRAIL_ENABLED",
    "GUARDRAIL_BLOCK_SCORE",
    # 长期记忆：关掉的话 injection-memory 会给出一个假的满分
    "MEMORY_ENABLED",
    "MEMORY_INJECT_LIMIT",
    # 委派：每次委派多一整个嵌套子代理循环
    "AGENT_DELEGATION_MODE",
    "AGENT_MAX_DELEGATIONS",
    # 提示词
    "PROMPT_CHAT_SYSTEM_VERSION",
    # 语义缓存：命中一次就是 0 轮 0 调用满分，会把每个指标读成"完美且免费"
    "SEMANTIC_CACHE_ENABLED",
)


@pytest.mark.parametrize("field", BEHAVIOR_CRITICAL)
def test_base_pins_every_behavior_critical_switch(field: str):
    assert field in _BASE, f"{field} 没在 _BASE 里写死，本地 .env 能静默改掉它"


@pytest.mark.parametrize("field", BEHAVIOR_CRITICAL)
def test_pinned_switches_are_real_settings_fields(field: str):
    """写错名字的话 setattr 会安静地加一个没人读的属性。"""
    assert field in Settings.model_fields, f"Settings 上没有 {field}"


def test_every_base_key_is_a_real_settings_field():
    """_BASE 里的每一项都要真的对得上 Settings 的字段。

    ``run_variant`` 用 ``setattr(settings, key, value)`` 套用配置——拼错的键会
    安静地在对象上挂一个没人读的属性，变体看起来生效了但什么都没变。
    """
    for key in _BASE:
        assert key in Settings.model_fields, f"_BASE 里的 {key} 不是 Settings 字段"


def test_semantic_cache_is_off_everywhere():
    """唯一一个不能拿来做变体维度的开关：它不改变 Agent，它是绕过 Agent。"""
    for name, variant in AGENT_VARIANTS.items():
        assert variant.overrides.get("SEMANTIC_CACHE_ENABLED") is False, (
            f"{name} 没关语义缓存"
        )


def test_every_variant_derives_from_base():
    """变体只能覆盖 _BASE 里的键，不能漏掉其中任何一个。

    ``{**_BASE, ...}`` 这个写法保证了这件事，但下一个人手写一个 dict 就绕过去了
    ——而后果同样是一份看不出异常的报告。
    """
    for name, variant in AGENT_VARIANTS.items():
        missing = set(_BASE) - set(variant.overrides)
        assert not missing, f"{name} 少了 {sorted(missing)}"


def test_delegation_variants_switch_the_prompt_with_the_mode():
    """模式和提示词必须配对，否则 main.py 的启动校验会直接拒。

    这条同时也验证了那套校验和 eval 配置说的是同一件事。
    """
    expected = {
        "augment": "v5-augment",
        "supervisor": "v6-supervisor",
        "off": None,
    }
    for name, variant in AGENT_VARIANTS.items():
        mode = variant.overrides.get("AGENT_DELEGATION_MODE")
        want = expected[mode]
        if want:
            assert variant.overrides["PROMPT_CHAT_SYSTEM_VERSION"] == want, (
                f"{name} 用 {mode} 模式，提示词该是 {want}"
            )


# ========== 澄清指标不能自相矛盾 ==========


def _outcome(**kw):
    """一份最小的 TurnOutcome，只填这几条测试要用的字段。"""
    from eval.agent_runner import TurnOutcome

    base = dict(
        question="q",
        answer="",
        calls=[],
        prefetch_calls=0,
        rounds=1,
        tool_recall=None,
        tool_precision=None,
        forbidden_hits=0,
        round_efficiency=None,
        order_ok=None,
        keyword_coverage=None,
        avoid_hits=0,
        repeated_calls=0,
        repeated_blocked=0,
        guardrail_hits=0,
        unavailable_calls=0,
        invalid_calls=0,
        errors=[],
        prompt_tokens=0,
        completion_tokens=0,
        cost=None,
        currency=None,
        unpriced_models=set(),
        latency_ms=0,
    )
    base.update(kw)
    return TurnOutcome(**base)


def test_never_asking_cannot_count_as_resumed():
    """没问过澄清，就不可能"接上"——这一列必须是 0。

    2026-08-30 实测踩到的：模型压根没调 ``ask_user``，自己拿一个假设把任务做完了。
    第一版 ``clarificationResumed`` 只判"答案非空"，于是报了「2 条都接上了」，
    而同一份报告里 ``clarificationAsked`` 是 0。两个数直接矛盾，而那一列是假的。

    这是这个项目反复出现的一种指标缺陷：**指标在那件事从没发生时也有值**
    （同 fabricationRate 的 substring 漏判、拒答裁判的 abstained 自相矛盾）。
    判据必须带上"前提成立"这一半。
    """
    from eval import agent_runner

    asked = _outcome(answer="接上之后的正文", clarification_requests=1)
    never = _outcome(answer="没问就直接答完了", clarification_requests=0)

    assert agent_runner._resumed_count([asked, never]) == 1, (
        "没问过的那条不能算接上"
    )
    assert agent_runner._resumed_count([never]) == 0
    # 问了但答案是空的也不算：那是接续真的失败了
    assert agent_runner._resumed_count([_outcome(clarification_requests=1)]) == 0


class _FakeVerdict:
    def __init__(self, success):
        self.success = success
        self.grounded = None
        self.failed = False
        self.fabricated_tool_output = False


class _FakeTask:
    def __init__(self, turns):
        self.turns = turns
        self.probe = "clarification"


class _FakeResult:
    def __init__(self, success, turn_specs, turn_outcomes):
        self.verdict = _FakeVerdict(success)
        self.task = _FakeTask(turn_specs)
        self.turns = turn_outcomes


def test_裁判高分配上必需内容缺失算矛盾():
    """裁判说做到了，而 must_include 一个都没命中——那个分数被确定性判据证伪。

    2026-09-01 实测原话：裁判给 5.0、理由写「问了城市等级后给出唯一数字900
    （450×2）」，而答案结尾就是那句问话，900 压根不在里面。裁判把"它接下来
    应该会算出 900"写成了"它算出了 900"。
    """
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    spec = TurnSpec(question="q", must_include=["900"])
    contradicting = _FakeResult(5.0, [spec], [_outcome(keyword_coverage=0.0)])
    assert agent_runner._judge_contradictions([contradicting]) == 1


def test_命中了就不算矛盾():
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    spec = TurnSpec(question="q", must_include=["900"])
    ok = _FakeResult(5.0, [spec], [_outcome(keyword_coverage=1.0)])
    assert agent_runner._judge_contradictions([ok]) == 0


def test_低分不算矛盾():
    """裁判自己就给了低分，那和确定性判据是一致的，不是矛盾。"""
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    spec = TurnSpec(question="q", must_include=["900"])
    low = _FakeResult(1.0, [spec], [_outcome(keyword_coverage=0.0)])
    assert agent_runner._judge_contradictions([low]) == 0


def test_没声明必需内容的用例不进这个判据():
    """抽取类用例判据是确定性的、不叫裁判，没有 must_include 可比。"""
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    spec = TurnSpec(question="q")
    none = _FakeResult(5.0, [spec], [_outcome(keyword_coverage=None)])
    assert agent_runner._judge_contradictions([none]) == 0


def test_多轮任务不能按摊平下标对齐():
    """``verdict.success`` 是每任务一个，``must_include`` 是每轮一个。

    第一版写的是 ``zip(graded, pairs)``，而 ``pairs`` 是所有任务的轮次摊平之后的
    列表——只要有一个任务是多轮的，后面全部错位，于是拿 A 任务的裁判分去配 B 任务
    的命中率。数据集里 multi_domain 那几条就是多轮的。

    这里用"第一个任务两轮、第二个任务一轮"钉住：摊平对齐的实现会把第二个任务的
    裁判分配到第一个任务的第二轮上，从而给出不同的答案。
    """
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    two_turn = _FakeResult(
        5.0,
        [TurnSpec(question="a"), TurnSpec(question="b", must_include=["900"])],
        [_outcome(keyword_coverage=None), _outcome(keyword_coverage=0.0)],
    )
    one_turn = _FakeResult(
        1.0, [TurnSpec(question="c", must_include=["1350"])], [_outcome(keyword_coverage=0.0)]
    )

    # 第一个任务:高分 + 第二轮必需内容缺失 → 矛盾。第二个:低分 → 不算。
    assert agent_runner._judge_contradictions([two_turn, one_turn]) == 1


def test_一个任务里多轮缺失只记一次():
    """判的是"这个任务的裁判分不可用",不是"缺了几处"。"""
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    result = _FakeResult(
        4.0,
        [TurnSpec(question="a", must_include=["1"]), TurnSpec(question="b", must_include=["2"])],
        [_outcome(keyword_coverage=0.0), _outcome(keyword_coverage=0.0)],
    )
    assert agent_runner._judge_contradictions([result]) == 1


def test_非澄清用例上的收编算误判():
    """判据误伤：正常回答被当成提问，本该 done 的回合挂成了 waiting_input。

    其余三个澄清指标全按 ``spec.clarification_answer`` 过滤（"这条用例准备了
    答案"），所以误判**天然落在它们的分母外面**——32 条全量跑下来，30 条非澄清
    用例上的误伤在报告里一个字都看不到，只会以 clarification_unanswered 的形式
    间接冒出来，而那条错误原因同时也覆盖"模型该问却没问"。

    这一列是 CLARIFY_ADOPT_PROSE_QUESTION 能不能默认打开的唯一判据。
    """
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    misfire = _FakeResult(
        5.0, [TurnSpec(question="q")], [_outcome(clarification_adopted=1)]
    )
    assert agent_runner._unexpected_adoptions([misfire]) == 1


def test_澄清用例上的收编不算误判():
    """那是这个功能该做的事，不是误伤。"""
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    intended = _FakeResult(
        5.0,
        [TurnSpec(question="q", clarification_answer="二线城市")],
        [_outcome(clarification_adopted=1)],
    )
    assert agent_runner._unexpected_adoptions([intended]) == 0


def test_裁判失败不该把误收编藏起来():
    """用 results 而不是 graded：两件事独立。

    一条用例可以既裁判失败、又误收编。按 graded 过滤的话那次误伤就消失了，
    而"开关能不能默认打开"这个结论恰恰取决于它。
    """
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    failed = _FakeResult(
        None, [TurnSpec(question="q")], [_outcome(clarification_adopted=1)]
    )
    failed.verdict.failed = True
    assert agent_runner._unexpected_adoptions([failed]) == 1


def test_收编次数不能混进模型主动提问():
    """``clarificationAsked`` 与 ``clarificationAdopted`` 必须分开。

    合成一个数之后，打开 CLARIFY_ADOPT_PROSE_QUESTION 会让 clarificationAsked
    从 0 跳到 2，报告读起来像"模型终于学会调 ask_user 了"——而它一次都没调，
    那 2 次是框架从回答正文里接住的。两者的处置相反：前者说明提示词/工具面对了，
    后者说明只能靠框架兜。
    """
    from eval.agent_runner import TurnOutcome

    adopted = _outcome(clarification_requests=1, clarification_adopted=1)
    self_asked = _outcome(clarification_requests=1, clarification_adopted=0)

    assert isinstance(adopted, TurnOutcome)
    # 模型自己调的次数 = asked - adopted
    assert adopted.clarification_requests - adopted.clarification_adopted == 0
    assert self_asked.clarification_requests - self_asked.clarification_adopted == 1


def test_收编来的也算接上了():
    """接续判据看的是"问过 + 答案非空",收编来的那次同样满足前一半。

    收编的目的就是让那一轮能接着跑,所以它必须能进 clarificationResumed——
    否则报告会显示"收编了 2 次、接上 0 条",看起来像回灌那条路断了。
    """
    from eval import agent_runner

    adopted = _outcome(
        answer="接上之后的正文", clarification_requests=1, clarification_adopted=1
    )
    assert agent_runner._resumed_count([adopted]) == 1


def test_出错原因按次数归类():
    """``turnErrors`` 只是计数,"出错轮次 2" 读不出该做什么。

    2026-08-31 实测两个变体都是 2,原因全是 clarification_never_asked——功能没被
    走进去,不是跑崩了。这两种情况在计数上同形而处置相反。
    """
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    spec = TurnSpec(question="q")
    pairs = [
        (spec, _outcome(errors=["clarification_never_asked"])),
        (spec, _outcome(errors=["clarification_never_asked"])),
        (spec, _outcome(errors=["model_error"])),
    ]
    reasons = agent_runner._error_reasons(pairs)
    assert reasons == ["clarification_never_asked ×2", "model_error ×1"], (
        "按次数降序,次数必须带上——只列种类看不出规模"
    )


def test_没出错时返回None而不是空列表():
    """空列表和 None 在渲染层是两回事：None 才让整段消失。"""
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    assert agent_runner._error_reasons([(TurnSpec(question="q"), _outcome())]) is None


def test_冒号后的可变部分归到同一类():
    """``unknown_approval_verdict:xxx`` 每条用例的后缀都不同,不归类就每条各成一类。"""
    from eval import agent_runner
    from eval.agent_runner import TurnSpec

    spec = TurnSpec(question="q")
    pairs = [
        (spec, _outcome(errors=["unknown_approval_verdict:maybe"])),
        (spec, _outcome(errors=["unknown_approval_verdict:later"])),
    ]
    assert agent_runner._error_reasons(pairs) == ["unknown_approval_verdict ×2"]


# ========== 磁盘状态检查 ==========
#
# 2026-09-11 加。其余所有判据看的都是**模型说了什么**——答案文本、工具调用序列、
# 裁判的印象。写操作是唯一会改变工作区状态的动作，而"它说写好了"和"文件真的变成
# 了那样"是两件事。差一点的情形不是模型撒谎，是路径拼错、写到了别处、或者 content
# 被截断——三种都会让答案看起来完全正常。


def _task(**kwargs):
    from eval.agent_runner import AgentTask

    base = dict(id="t", probe="p", rubric="r", turns=[])
    base.update(kwargs)
    return AgentTask(**base)


def test_内容一致时没有错误(tmp_path):
    from eval.agent_runner import _check_workspace_after

    (tmp_path / "a.md").write_text("期望内容\n", encoding="utf-8")
    task = _task(workspace_after={"a.md": "期望内容"})

    assert _check_workspace_after(None, task, str(tmp_path)) == []


def test_内容不符时报出来(tmp_path):
    """这是这一组存在的理由：模型声称写好了，而文件不是那样。"""
    from eval.agent_runner import _check_workspace_after

    (tmp_path / "a.md").write_text("其实没改", encoding="utf-8")
    task = _task(workspace_after={"a.md": "期望内容"})

    errors = _check_workspace_after(None, task, str(tmp_path))
    assert len(errors) == 1 and "内容不符" in errors[0]


def test_结尾换行不算差异(tmp_path):
    """模型给的内容结尾多不多一个换行不是判据。"""
    from eval.agent_runner import _check_workspace_after

    (tmp_path / "a.md").write_text("内容\n\n", encoding="utf-8")
    task = _task(workspace_after={"a.md": "内容"})

    assert _check_workspace_after(None, task, str(tmp_path)) == []


def test_文件缺失时报出来(tmp_path):
    from eval.agent_runner import _check_workspace_after

    task = _task(workspace_after={"missing.md": "内容"})
    errors = _check_workspace_after(None, task, str(tmp_path))
    assert len(errors) == 1 and "不存在" in errors[0]


def test_期望为None表示应当已被删除(tmp_path):
    from eval.agent_runner import _check_workspace_after

    task = _task(workspace_after={"gone.md": None})
    # 文件确实不在 → 通过
    assert _check_workspace_after(None, task, str(tmp_path)) == []

    # 文件还在 → 失败
    (tmp_path / "gone.md").write_text("还在", encoding="utf-8")
    errors = _check_workspace_after(None, task, str(tmp_path))
    assert len(errors) == 1 and "应当已被删除" in errors[0]


def test_越界路径被挡掉(tmp_path):
    """夹具是我们自己写的，但一个手误的 `../` 会让检查去读工作区外面的文件。"""
    from eval.agent_runner import _check_workspace_after

    task = _task(workspace_after={"../outside.md": "x"})
    errors = _check_workspace_after(None, task, str(tmp_path))
    assert len(errors) == 1 and "越界" in errors[0]


def test_声明了恢复却没有备份时算失败(tmp_path, monkeypatch):
    """restore_latest 的前提是那次写真的留下了旧版本。没有就是缺陷本身。"""
    from config import settings
    from eval.agent_runner import _check_workspace_after
    from services import fs_backup

    monkeypatch.setattr(settings, "FS_BACKUP_DIR", str(tmp_path / "_b"))
    monkeypatch.setattr(fs_backup, "list_for_user", lambda db, uid: [])

    task = _task(restore_latest=True)
    errors = _check_workspace_after(None, task, str(tmp_path))
    assert any("没有任何可恢复的备份" in e for e in errors)
