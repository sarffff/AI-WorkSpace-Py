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
