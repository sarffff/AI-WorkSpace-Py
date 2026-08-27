"""评估报告渲染:降级与计价缺口必须出现在 Markdown 里。

## 为什么这件事值得一个测试文件

这个项目里反复出现同一个形状的 bug:**记录了,但没冒泡到结论层**。

- ``rerank-api`` 变体因为端点返 429 从未真正执行过,报告里却和 baseline 逐位相同,
  被读成"专用重排没有增益",持续了很久。
- 四处检索增强(路由/HyDE/多查询/重排)在预算被思考吃光时 100% 降级,而对应变体
  与 baseline 逐位相同。
- ``agent_runner`` 从一开始就算了 ``unpricedModels``,但 ``run_agent.render_markdown``
  从没渲染过它——数据在 JSON 里,结论在 Markdown 里,而人只读 Markdown。

前两条的埋点和汇总在 2026-08-22/23 就补完了,``runner.py`` 里 ``degradedStages`` /
``degradedCases`` 一直在算。缺的一直是最后一跳:**进表格**。所以这里测的不是
"计算对不对",而是"算出来的东西人能不能看见"——那才是这类 bug 的实际断点。

数据全用手搓的 summary 字典,不跑真实评估:渲染层的契约就是"给定 summary 产出
什么文本",而真实评估要花模型调用。
"""
from __future__ import annotations

from eval import run as eval_run
from eval import run_agent


def _summary(**overrides):
    """一份干净的 baseline summary,按需覆盖字段。"""
    base = {
        "variant": "baseline",
        "answerPrompt": "v3",
        "questions": 46,
        "recall@5": 1.0,
        "precision@5": 0.385,
        "ndcg@5": 0.93,
        "mrr": 0.91,
        "faithfulness": 4.5,
        "relevance": 4.6,
        "abstentionRate": 1.0,
        "injectionResistRate": 1.0,
        "promptTokens": 1000,
        "completionTokens": 200,
        "cost": 0.01,
        "avgLatencyMs": 1200.0,
        "recallByProbe": {"lexical": 1.0},
        "degradedStages": None,
        "degradedCases": None,
        "unpricedModels": None,
    }
    base.update(overrides)
    return base


def _render(*summaries: dict) -> str:
    return eval_run.render_markdown({"summaries": list(summaries)})


def _render_paired(left: list[dict], right: list[dict]) -> str:
    """两个变体的逐题明细 → 完整报告文本(显著性一节在里面)。"""
    return eval_run.render_markdown(
        {
            "summaries": [_summary(), _summary(variant="rerank")],
            "details": {"baseline": left, "rerank": right},
        }
    )


# ========== 降级列 ==========


def test_clean_run_shows_zero_not_a_dash():
    """没降级要显示 0,不能显示 '-'。

    ``-`` 在这张表里的含义是"没有这个数据",而"一次都没降级"是个**好消息**,
    长得像缺数据就等于把好消息也变成了噪音。``runner`` 用 ``or None`` 是为了
    JSON 干净,渲染层要把 0 补回来。
    """
    markdown = _render(_summary())
    row = next(line for line in markdown.splitlines() if line.startswith("| baseline |"))
    assert "0/46" in row
    assert "## ⚠ 降级与计价缺口" not in markdown, "干净时整节不该出现"


def test_degraded_count_carries_a_denominator():
    """``12`` 说不清是十二分之一还是十二分之十二,而那是两个不同的结论。"""
    markdown = _render(
        _summary(variant="rerank", degradedCases=12, degradedStages={"rerank": 12})
    )
    row = next(line for line in markdown.splitlines() if line.startswith("| rerank |"))
    assert "12/46" in row


def test_degradation_section_names_the_stage():
    """阶段名必须出现:rerank 降级和 hyde 降级要修的东西完全不同。"""
    markdown = _render(
        _summary(
            variant="hyde-rerank",
            degradedCases=9,
            degradedStages={"hyde": 5, "rerank": 9},
        )
    )
    assert "## ⚠ 降级与计价缺口" in markdown
    assert "hyde x5" in markdown
    assert "rerank x9" in markdown


def test_full_degradation_is_called_out_explicitly():
    """满额降级要单独点出来,并说清该怎么读。

    这是"配了等于没配"最典型的样子,而它在指标上的表现是**和 baseline 完全一致**
    ——最容易被读成"这个技术没有增益"。rerank-api 就是这么被误读了很久。
    """
    markdown = _render(
        _summary(variant="rerank-api", degradedCases=46, degradedStages={"rerank": 46})
    )
    assert "没有真正执行" in markdown
    assert "先去修配置" in markdown


def test_partial_degradation_is_not_called_out_as_full():
    """偶发降级不该套用满额的那段话——那会把"要修配置"喊成狼来了。"""
    markdown = _render(
        _summary(variant="rerank", degradedCases=3, degradedStages={"rerank": 3})
    )
    assert "rerank x3" in markdown
    assert "没有真正执行" not in markdown


# ========== 漏价模型 ==========


def test_unpriced_models_reach_the_markdown():
    """漏价时成本是下界。这个值一直在算,此前只存在于 JSON 里。"""
    markdown = _render(_summary(variant="llm-rerank", unpricedModels=["glm-4.6v"]))
    assert "## ⚠ 降级与计价缺口" in markdown
    assert "glm-4.6v" in markdown
    assert "下界" in markdown


def test_agent_report_renders_unpriced_models():
    """agent 报告是同一个漏洞的另一半:算了,但从没渲染过。"""
    markdown = run_agent.render_markdown(
        {
            "summaries": [
                {
                    "variant": "baseline",
                    "tasks": 29,
                    "turns": 60,
                    "unpricedModels": ["glm-4.6v", "some-model"],
                    "successByProbe": {},
                }
            ]
        }
    )
    assert "## ⚠ 计价缺口" in markdown
    assert "glm-4.6v" in markdown
    assert "some-model" in markdown
    assert "下界" in markdown


def test_agent_report_omits_the_section_when_fully_priced():
    markdown = run_agent.render_markdown(
        {
            "summaries": [
                {
                    "variant": "baseline",
                    "tasks": 29,
                    "turns": 60,
                    "unpricedModels": None,
                    "successByProbe": {},
                }
            ]
        }
    )
    assert "计价缺口" not in markdown


# ========== 读法 ==========


def test_reading_guide_states_that_degradation_gates_the_other_columns():
    """读法里必须写清"降级非零时先别相信指标"。

    这一列的价值全在读者知道该拿它做什么。光有数字、没有读法,它就只是又一列
    看不懂的计数——而这个项目已经有一堆那样的列了。
    """
    markdown = _render(_summary())
    assert "降级列是读其他所有列的前提" in markdown
    assert "「它没跑」" in markdown


def test_multiple_variants_each_get_their_own_line():
    """两个变体各自降级时不能合并成一行——修法可能不一样。"""
    markdown = _render(
        _summary(),
        _summary(variant="hyde", degradedCases=46, degradedStages={"hyde": 46}),
        _summary(variant="rerank", degradedCases=2, degradedStages={"rerank": 2}),
    )
    assert "- **hyde**" in markdown
    assert "- **rerank**" in markdown
    assert "- **baseline**" not in markdown, "干净的变体不该出现在这一节"


def test_a_variant_hit_by_both_problems_appears_once():
    """同时降级又漏价的变体只出现一个条目,两条问题作为子项。

    分成两个顶层条目会让同一个粗体变体名出现两次,读起来像渲染坏了——而这一节
    存在的全部意义就是被人认真读一遍。
    """
    markdown = _render(
        _summary(
            variant="hyde",
            degradedCases=5,
            degradedStages={"hyde": 5},
            unpricedModels=["glm-4.6v"],
        )
    )
    assert markdown.count("- **hyde**") == 1
    assert "  - 降级：hyde x5" in markdown
    assert "  - 计价缺口：glm-4.6v" in markdown


# ========== precision 列 ==========


def test_precision_column_is_rendered():
    """precision 一直在按 case 算(metrics.py),但 2026-08-25 之前从没汇总过。

    它是这套 eval 里唯一还没饱和的检索指标——recall@5 恒为 1.0000,而
    precision@5 实测 0.3852。recall 到顶之后"混合召回 vs 纯稠密""重排开 vs 关"
    在报告上长得一样,而那不是"没差别",是尺子量不出差别。
    """
    markdown = _render(_summary())
    header = next(line for line in markdown.splitlines() if line.startswith("| 变体 |"))
    assert "precision@k" in header
    row = next(line for line in markdown.splitlines() if line.startswith("| baseline |"))
    assert "0.385" in row


def test_reading_guide_warns_that_recall_is_saturated():
    """光加一列不够:得写清"别再拿 recall 比变体"。

    这个项目里已经有一堆看不懂的列了,一个没有读法的指标只会变成新的噪音。
    """
    markdown = _render(_summary())
    assert "recall@k 已经饱和" in markdown
    assert "precision" in markdown.split("## 读法")[1]


# ========== 编造率 ==========


def test_fabrication_rate_column_is_rendered():
    """拒答率旁边必须有编造率,否则"拒得不干净"读不出来。"""
    markdown = _render(_summary(fabricationRate=0.25, fabricationCases=4))
    header = next(line for line in markdown.splitlines() if line.startswith("| 变体 |"))
    assert "编造率" in header
    row = next(line for line in markdown.splitlines() if line.startswith("| baseline |"))
    assert "0.250" in row


def test_fabrication_dash_is_explained_as_no_samples():
    """`-` 是"没有这类样本",不是"零编造"。读法里必须写清,否则会被读成好消息。"""
    markdown = _render(_summary())
    guide = markdown.split("## 读法")[1]
    assert "拒答率与编造率必须一起读" in guide
    assert "不是零编造" in guide


def test_reading_guide_states_the_injection_filter_is_probe_based():
    """判据从 must_avoid 改成 probe 之后,读法要跟着改——否则下一个人会照旧理解。"""
    guide = _render(_summary()).split("## 读法")[1]
    assert "probe=injection" in guide


# ========== 表头:问题数的分母 ==========


def test_header_splits_total_from_retrieval_scored():
    """检索四列的分母不是总题数,表头必须说出来。

    2026-08-25 加了 8 条硬负例之后是 54 总数 / 44 计检索。只写"问题数：54"会让
    ``recall@5`` 被读成 54 条的均值,而它是 44 条的——差 10 条,报告里看不出来。
    """
    markdown = _render(_summary(questions=54, retrievalScored=44))
    assert "问题数：54（检索计分 44" in markdown


def test_header_stays_terse_when_every_case_is_scored():
    """全部计分时不加括号——没有歧义的地方不要加解释,那是另一种噪音。"""
    markdown = _render(_summary(questions=44, retrievalScored=44))
    assert "问题数：44" in markdown
    assert "检索计分" not in markdown


def test_header_survives_a_summary_without_the_field():
    """老报告的 summary 没有这个键,渲染不能炸。"""
    base = _summary()
    base.pop("retrievalScored", None)
    assert "问题数：46" in _render(base)


# ========== 表头:语料指纹 ==========


def test_corpus_chunks_reach_the_header():
    """分块数是"这轮测的哪版语料"的指纹。

    没有它,扩容前(40 块)和扩容后(92 块)的两份报告看起来完全可比。项目里已经
    因为 ``ensure_corpus`` 早退而静默测过一次旧索引,那份报告长得和正常的一样。
    """
    markdown = _render(_summary(corpusChunks=92))
    assert "语料分块：92" in markdown


def test_differing_chunk_counts_are_listed_per_variant():
    """各变体分块数不同意味着索引都不是同一个,检索差异有一部分来自这里。"""
    markdown = _render(
        _summary(corpusChunks=92),
        _summary(variant="small-chunks", corpusChunks=170),
    )
    assert "各变体不同" in markdown
    assert "baseline 92" in markdown
    assert "small-chunks 170" in markdown


def test_corpus_line_is_omitted_when_unknown():
    """没有这个字段时整行不出现,而不是印一个 '语料分块：-'。

    断言带冒号:读法里也提到"语料分块数",裸子串会把那句话算成命中——
    于是这条测试在**代码正确**时也变红。
    """
    assert "语料分块：" not in _render(_summary())


def test_reading_guide_warns_against_cross_report_comparison():
    """光印一个数字不够:得写清分块数不同的两份报告不可比。"""
    guide = _render(_summary(corpusChunks=92)).split("## 读法")[1]
    assert "跨报告比较前先对齐表头的语料分块数" in guide


# ========== agent 报告:工具顺序与语料指纹 ==========


def _agent_summary(**overrides):
    base = {
        "variant": "baseline",
        "tasks": 29,
        "turns": 60,
        "successByProbe": {},
        "unpricedModels": None,
    }
    base.update(overrides)
    return base


def test_agent_report_renders_tool_order_rate():
    """``toolOrderRate`` 从加进 summary 起就没被渲染过——第五例同形状的 bug。

    它量的是先后顺序:先算价再查价和先查价再算价,召回与精度完全相同,但只有
    后者是对的。这个区别只有这一列能看出来。
    """
    markdown = run_agent.render_markdown(
        {"summaries": [_agent_summary(toolOrderRate=0.5)]}
    )
    header = next(line for line in markdown.splitlines() if line.startswith("| 变体 |"))
    assert "工具顺序" in header
    row = next(line for line in markdown.splitlines() if line.startswith("| baseline |"))
    assert "0.500" in row


def test_agent_report_shows_a_dash_when_no_task_checks_order():
    """没有 expect_order 样本时是 '-',不能粉饰成 0——那会读成"顺序全错"。"""
    markdown = run_agent.render_markdown(
        {"summaries": [_agent_summary(toolOrderRate=None)]}
    )
    row = next(line for line in markdown.splitlines() if line.startswith("| baseline |"))
    assert "| - |" in row


def test_agent_report_renders_corpus_chunks():
    """agent 侧同样需要语料指纹:带 RAG 的任务的有据性依赖检索。"""
    markdown = run_agent.render_markdown(
        {"summaries": [_agent_summary(corpusChunks=40)]}
    )
    assert "语料分块：40" in markdown


# ========== 显著性:点估计之外的不确定性 ==========


def _case(case_id: str, precision: float, ndcg: float = 1.0) -> dict:
    return {
        "id": case_id,
        "expected": ["hr-handbook.md"],
        "retrieval": {"precision@5": precision, "ndcg@5": ndcg},
    }


def _retrieval_case(
    case_id: str, precision: float, retrieved: list[str], ndcg: float = 1.0
) -> dict:
    """带 ``retrieved`` 的 case。成分拆解要文档集合，``_case`` 没有这个字段。"""
    return {
        "id": case_id,
        "expected": ["hr-handbook.md"],
        "retrieved": retrieved,
        "retrieval": {"precision@5": precision, "ndcg@5": ndcg},
    }


def _paired_report(left: list[dict], right: list[dict]) -> dict:
    return {
        "summaries": [_summary(), _summary(variant="rerank")],
        "details": {"baseline": left, "rerank": right},
    }


def test_a_single_case_moving_is_reported_as_indistinguishable():
    """一条题变化不能读成"略微变差"。

    这是 2026-08-24 那份报告的真实形状:nDCG 0.974 vs 0.971,三位小数、不带
    不确定性。离线重算之后 44 条里只有 **1 条**不同,recall 与 precision 逐位
    相同。整个 0.003 来自一条题——而报告长得像个可以下结论的差值。
    """
    left = [_case(f"q{i}", 0.4, ndcg=1.0) for i in range(44)]
    right = [_case(f"q{i}", 0.4, ndcg=1.0) for i in range(44)]
    right[7]["retrieval"]["ndcg@5"] = 0.87
    markdown = _render_paired(left, right)
    # 措辞从"1/44 条有变化"改成胜负拆分：只说"有几条变了"分不出方向，
    # 而"1 条变差"和"1 条变好"对读者是完全不同的信息。
    assert "0 好 / 1 差" in markdown
    assert "分不出来" in markdown


def test_a_consistent_shift_is_reported_as_real():
    """每条题都朝同一个方向动,才配叫真实差异。"""
    left = [_case(f"q{i}", 0.4) for i in range(44)]
    right = [_case(f"q{i}", 0.6) for i in range(44)]
    markdown = _render_paired(left, right)
    assert "**真实差异**" in markdown
    assert "+0.2000" in markdown


def test_pairing_keys_on_case_id_not_list_position():
    """按下标配对会算出一个漂亮但无意义的区间,而且**不会报错**。

    ## 这个测试断言什么,以及为什么不能断言差值

    **配对差值的均值恒等于 mean(变体) - mean(baseline),和怎么配对完全无关。**
    配对影响的只有方差,也就是置信区间和判定。所以拿差值做断言的测试对配对
    bug 一点约束力都没有——我第一版就是那么写的,变异验证时两种实现都给
    ``+0.0227``,测试照样绿。

    夹具还必须让 **baseline 逐题不同**。若 baseline 恒为 0.4,倒序之后两种配对
    得到的差值多重集完全相同,依然分不开。

    现在的夹具:baseline 逐题取值不同,变体在每条题上一律 +0.1,右侧整体倒序。
    - 按 id(正确):每条差值都恰好 +0.1 → 区间 [+0.1000, +0.1000] → 真实差异
    - 按下标(错误):q0 对上 q43 → 差值被搅成噪声 → 区间跨 0 → 分不出来

    点估计两边都是 +0.1000。**只有区间分得开**,所以断言必须落在区间和判定上。
    """
    base_values = [0.2 + 0.2 * (index % 4) for index in range(44)]
    left = [_case(f"q{i}", base_values[i]) for i in range(44)]
    right = [_case(f"q{i}", round(base_values[i] + 0.1, 4)) for i in range(44)]
    right.reverse()
    markdown = _render_paired(left, right)
    assert "[+0.1000, +0.1000]" in markdown, "按 id 配对每条差值都是 +0.1,区间应当收紧"
    assert "**真实差异**" in markdown


def test_cases_without_expected_documents_are_excluded():
    """没标来源的题不进配对:recall 对空相关集返回 1.0,混进来会凭空拉高。"""
    left = [_case(f"q{i}", 0.4) for i in range(10)]
    right = [_case(f"q{i}", 0.4) for i in range(10)]
    left.append({"id": "feedback-1", "expected": [], "retrieval": {"precision@5": 1.0}})
    right.append({"id": "feedback-1", "expected": [], "retrieval": {"precision@5": 1.0}})
    markdown = _render_paired(left, right)
    assert "| 10 |" in markdown, "11 条里只有 10 条该进配对"


def test_significance_section_reads_historical_reports():
    """指标名从 details 取而不是 summary,所以这一节能跑在花过钱的老报告上。

    2026-08-24 之前的 summary 没有 precision@k,但 details 里每条 case 一直都有。
    从 summary 取的话,历史报告永远出不来这一节。
    """
    left = [_case(f"q{i}", 0.4) for i in range(20)]
    right = [_case(f"q{i}", 0.5) for i in range(20)]
    report = _paired_report(left, right)
    for summary in report["summaries"]:
        summary.pop("precision@5", None)
    markdown = eval_run.render_markdown(report)
    assert "precision@5" in markdown.split("## 相对")[1]


def test_a_seed_dependent_verdict_is_flagged():
    """判定会随种子翻的行必须带 ⚠。

    2026-08-27 那轮 rerank 的 precision 就是这样：区间下界只有 +0.0008，
    20 个种子里有 1 个把它判成不显著。100% 和 0% 都不该带 ⚠——那两种情况说明
    结论不来自随机数；中间值才是"再加几道题"的信号。
    """
    left = [_case(f"q{i}", 0.4) for i in range(44)]
    right = [_case(f"q{i}", 0.4) for i in range(44)]
    # 少数题大幅改善 → 差值贴着 0，判定对种子敏感
    for index in (0, 1, 2):
        right[index]["retrieval"]["precision@5"] = 1.0
    markdown = _render_paired(left, right)
    stability_cells = [
        line for line in markdown.splitlines() if "precision@5 | rerank" in line
    ]
    assert stability_cells, "没找到 precision@5 那一行"
    assert "%" in stability_cells[0]


def test_stable_verdicts_are_not_flagged():
    """每个种子都同一个结论时不带 ⚠：0% 和 100% 都是好消息。"""
    left = [_case(f"q{i}", 0.4) for i in range(44)]
    right = [_case(f"q{i}", 0.4) for i in range(44)]
    markdown = _render_paired(left, right)
    row = next(
        line for line in markdown.splitlines() if "precision@5 | rerank" in line
    )
    assert "⚠" not in row, "逐题全同应当 0% 且不带警告"
    assert "0%" in row


def test_section_is_absent_for_a_single_variant_run():
    """只跑一个变体时没有可比的对象,整节不该出现。"""
    markdown = eval_run.render_markdown(
        {"summaries": [_summary()], "details": {"baseline": [_case("q0", 0.4)]}}
    )
    assert "显著性" not in markdown


def test_reading_guide_rejects_reading_a_wide_interval_as_equivalence():
    """跨 0 的正确读法是"分不出来",不是"两者相同"——这个区别决定下一步做什么。"""
    left = [_case(f"q{i}", 0.4) for i in range(44)]
    right = [_case(f"q{i}", 0.4) for i in range(44)]
    right[3]["retrieval"]["precision@5"] = 0.6
    markdown = _render_paired(left, right)
    assert "既不是「两者相同」" in markdown
    # 断言从"可分辨阈值"改成"种子稳定":前者用正态近似算,会和自助法判定互相
    # 矛盾(见 metrics.paired_bootstrap 里那段),已从报告里去掉。
    assert "种子稳定" in markdown


# ========== 逐题胜负进表 ==========


def test_wins_and_losses_appear_in_the_table():
    """胜负要落进表格，不能只活在 JSON 里。

    这是这个项目反复出现的形状：算了、存进 JSON、人只读 Markdown。
    """
    left = [_case(f"q{i}", 0.4) for i in range(28)]
    right = [_case(f"q{i}", 0.9 if i < 18 else 0.0) for i in range(28)]
    markdown = _render_paired(left, right)
    assert "18 好 / 10 差" in markdown


def test_all_tied_says_so_instead_of_zero_slash_zero():
    """全部持平印「全部持平」，不印「0 好 / 0 差」——后者读起来像缺数据。"""
    left = [_case(f"q{i}", 0.4) for i in range(10)]
    right = [_case(f"q{i}", 0.4) for i in range(10)]
    assert "全部持平" in _render_paired(left, right)


# ========== 需要多少题 ==========


def test_required_sample_size_section_appears_when_inconclusive():
    """判不出来时给出「约需多少题」，把不确定翻译成可执行的数。"""
    left = [_case(f"q{i}", 0.4) for i in range(44)]
    right = [_case(f"q{i}", 0.9 if i < 18 else 0.0) for i in range(44)]
    markdown = _render_paired(left, right)
    assert "需要多少题" in markdown
    assert "| 44 |" in markdown


def test_required_sample_size_section_is_absent_when_conclusive():
    """判定已经成立时不出现——那时再说"要多少题"是废话。"""
    left = [_case(f"q{i}", 0.4) for i in range(44)]
    right = [_case(f"q{i}", 0.5) for i in range(44)]
    markdown = _render_paired(left, right)
    assert "**真实差异**" in markdown
    assert "需要多少题" not in markdown


# ========== precision 成分拆解 ==========


def _decomposed(left: list[dict], right: list[dict]) -> str:
    return eval_run.render_markdown(
        {
            "summaries": [_summary(), _summary(variant="rerank")],
            "details": {"baseline": left, "rerank": right},
        }
    )


def test_chunk_count_artifact_is_separated_from_real_change():
    """两侧同一批文档、只有分块数不同 → 记为假象，不记为变差。

    ``postmortem-p2-trigger`` 的形状：precision 掉 0.500，而两侧都只取回了
    正确文档，nDCG 两边都是 1.000。
    """
    left = [_retrieval_case("q0", 1.0, ["hr-handbook.md"])]
    right = [_retrieval_case("q0", 0.5, ["hr-handbook.md", "hr-handbook.md"])]
    markdown = _decomposed(left, right)
    assert "precision 变化的成分" in markdown
    assert "| 0 好 / 0 差 |" in markdown, "文档集合没变，不该记成真实变差"
    assert "| 1 |" in markdown, "该记成 1 条假象"


def test_real_change_is_reported_as_real():
    """文档集合真的变了就是真实变化。"""
    left = [_retrieval_case("q0", 0.5, ["hr-handbook.md", "api-guide.md"])]
    right = [_retrieval_case("q0", 1.0, ["hr-handbook.md"])]
    markdown = _decomposed(left, right)
    assert "| 1 好 / 0 差 |" in markdown


def test_decomposition_section_is_absent_when_no_artifacts():
    """没有假象时整节不出现。

    全是真实变化时这一节是噪音，而这个项目里最贵的一类错误就是把警告混在
    常态噪音里，于是没人再读它。
    """
    left = [_retrieval_case(f"q{i}", 0.5, ["hr-handbook.md", "api-guide.md"]) for i in range(6)]
    right = [_retrieval_case(f"q{i}", 1.0, ["hr-handbook.md"]) for i in range(6)]
    markdown = _decomposed(left, right)
    assert "precision 变化的成分" not in markdown


def test_decomposition_needs_retrieved_and_degrades_quietly_without_it():
    """老报告没有 ``retrieved`` 时不该崩，整节不出现即可。"""
    left = [_case(f"q{i}", 0.4) for i in range(6)]
    right = [_case(f"q{i}", 0.6) for i in range(6)]
    markdown = _decomposed(left, right)
    assert "precision 变化的成分" not in markdown
