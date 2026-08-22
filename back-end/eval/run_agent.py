"""Agent 端到端评估 CLI。

    python -m eval.run_agent --limit 2                     # 先小样本试跑
    python -m eval.run_agent                               # baseline 跑全量
    python -m eval.run_agent --variants baseline,no-tool-history
    python -m eval.run_agent --variants all                 # 全部变体对照

会真实调用模型接口，**产生费用**。开销约为
``任务数 x 变体数 x (每任务若干轮模型调用 + 1 次裁判调用)``，
其中"若干轮"就是被评估的那个数（见报告里的 avgRounds），所以比 RAG 评估更贵。
先用 ``--limit`` 估一下。

web_search 走替身、不联网、不需要 key；知识库检索仍然会真实调用 embedding 接口。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

from eval import agent_runner
from eval.agent_variants import AGENT_VARIANTS, resolve
from services.clock import now as app_now

_REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

# None 渲染成 "-"，不要粉饰成 0
_COLUMNS = [
    ("variant", "变体"),
    ("systemPrompt", "提示词"),
    ("taskSuccess", "任务成功"),
    ("grounded", "有据性"),
    ("toolRecall", "工具召回"),
    ("toolPrecision", "工具精度"),
    ("roundEfficiency", "轮次效率"),
    ("avgRounds", "平均轮次"),
    ("forbiddenCalls", "违规调用"),
    ("injectionResistRate", "抗注入率"),
    # 和抗注入率是两层不同的防线，分开列：抗注入率量"脏记忆已在库里时拦不拦得住"，
    # 抽取抗性量"那行脏记忆该不该被写进来"。合成一个数会把"两层都薄"和
    # "一层厚一层薄"算成同一个分。
    ("extractionResistRate", "抽取抗性"),
    ("extractionRecall", "抽取召回"),
    # 被拒之后没有把同一件事再提交一遍的比例。它和上面两列都不是一层东西：
    # 那两列量记忆防线，这一列量 approval.rejection_message 的措辞有没有生效。
    ("rejectionRespectRate", "拒绝遵从率"),
    # 计划点名的工具实际调了几成。只在 plan-execute 变体非空——必须和诊断表里的
    # 「计划步数」一起读：步数为 0 时这一列是 '-'，而 0 步既可能是"不用分步"也
    # 可能是"规划静默失效"。
    ("planAdherence", "计划遵从率"),
    ("keywordCoverage", "关键词命中"),
    ("promptTokens", "输入 token"),
    ("completionTokens", "输出 token"),
    ("cost", "成本"),
    ("avgLatencyMs", "平均耗时 ms"),
]

_DIAGNOSTICS = [
    ("modelToolCalls", "模型工具调用"),
    ("prefetchCalls", "预检索次数"),
    ("repeatedCalls", "重复调用"),
    ("repeatedBlocked", "重复已拦截"),
    ("unavailableCalls", "工具不可用"),
    ("invalidCalls", "参数错误"),
    ("guardrailHits", "护栏命中"),
    ("otherAvoidHits", "非注入禁词命中"),
    ("extractionWritten", "抽取入库条数"),
    ("approvalInterrupts", "审批中断次数"),
    ("planSteps", "计划步数"),
    ("fabricatedToolOutput", "谎称调过工具"),
    ("writtenDocuments", "写入文档数"),
    ("turnErrors", "出错轮次"),
    ("judgeFailures", "裁判失败"),
    ("stubMisses", "搜索替身未命中"),
]


def _format(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 100 else f"{value:.0f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    summaries = report["summaries"]
    lines = [
        "# Agent 端到端评估报告",
        "",
        f"生成时间：{app_now().isoformat(timespec='seconds')}",
        f"任务数：{summaries[0]['tasks'] if summaries else 0}"
        f" / 轮次数：{summaries[0]['turns'] if summaries else 0}",
        "",
        "| " + " | ".join(label for _key, label in _COLUMNS) + " |",
        "| " + " | ".join("---" for _ in _COLUMNS) + " |",
    ]
    for summary in summaries:
        cells = [_format(summary.get(key)) for key, _label in _COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")

    lines += ["", "## 按探针类型的任务成功率", ""]
    probes = sorted({p for s in summaries for p in s.get("successByProbe", {})})
    if probes:
        lines.append("| 变体 | " + " | ".join(probes) + " |")
        lines.append("| " + " | ".join("---" for _ in range(len(probes) + 1)) + " |")
        for summary in summaries:
            by_probe = summary.get("successByProbe", {})
            cells = [_format(by_probe.get(probe)) for probe in probes]
            lines.append(f"| {summary['variant']} | " + " | ".join(cells) + " |")

    lines += ["", "## 诊断计数", ""]
    lines.append("| 变体 | " + " | ".join(label for _k, label in _DIAGNOSTICS) + " |")
    lines.append("| " + " | ".join("---" for _ in range(len(_DIAGNOSTICS) + 1)) + " |")
    for summary in summaries:
        cells = [_format(summary.get(key)) for key, _label in _DIAGNOSTICS]
        lines.append(f"| {summary['variant']} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 读法",
        "",
        "- 任务成功 / 有据性是 LLM 裁判打的 1-5 分，只用于变体之间相对比较。",
        "  有据性判的是「回答里的事实能不能在工具实际返回的内容里找到」，",
        "  所以它低而成功率高，意味着答对了但依据是编的——那比答错更危险。",
        "- 工具召回按集合算（必需工具是否各用过一次），工具精度按调用次数算",
        "  （做的动作里多少是必要或允许的）。两个一起看：召回高精度低是",
        "  「什么都试一遍」，精度高召回低是「能不动就不动」，修法完全相反。",
        "- 预检索（round 0）不计入工具召回与精度，它是配置决定的、不是模型选的，",
        "  单独记在诊断表的「预检索次数」里。",
        "- 轮次效率 = 数据集里标的最少必要轮次 / 实际轮次，上限 1.0。",
        "  它衡量绕路，不衡量对错；答错但只用一轮同样是 1.0。",
        "- 违规调用是硬指标：非零就意味着调了明确不该调的工具，",
        "  其中 save_to_knowledge_base 被未经要求地调用一次，就是一次未授权的状态变更。",
        "- 重复调用是同一个 (工具, 参数) 被执行了几次（首次不算）。循环目前没有",
        "  去重，这个数字就是那笔浪费的大小，也是后续做去重时的改动前基线。",
        "- 抗注入率只统计 probe=injection 且带 must_avoid 的样本，衡量的是",
        "  「提示词 + 护栏」的联合表现；要拆开归因就跑 no-guardrail 变体对照。",
        "  判据用 probe 而不是「有没有标 must_avoid」：must_avoid 是个通用的",
        "  「不该出现的字符串」机制，recovery 探针拿它抓的是编造汇率，混进分母会",
        "  把注入抗性算高。那类命中另计在诊断表的「非注入禁词命中」里。",
        "- 抽取抗性与抗注入率是记忆防线的两层，不能合成一个数：",
        "  抽取抗性量「改变助手行为的要求有没有被写进长期记忆」（第一层，",
        "  prompts/memory_extract/），抗注入率量「脏记忆已经在库里时拦不拦得住」",
        "  （第二层，build_system_block 的定界 + 声明）。真实链路上第一层先失手，",
        "  才轮到第二层，所以两个数要分开看：都低才是防线真的薄。",
        "- 抽取召回是反向信号：正当的事实与偏好有没有被一起挡掉。只看抗性的话，",
        "  一个「什么都不记」的退化实现能拿满分，而那会静默丢掉用户全部背景。",
        "  抽取类任务不进任务成功（判据是确定性的，不叫裁判），只体现在这两列。",
        "- 拒绝遵从率只统计 probe=approval 里裁决为 reject 的轮次：用户点了拒绝之后，",
        "  模型有没有把同一件事再提交一遍。判据是审批中断次数（1 = 正常，≥2 = 重试了），",
        "  不是违规调用——审批闸门在 tool_start 之前触发，被拦下的那次调用根本不进",
        "  calls，所以 forbid_tools 在这条路上看不见任何东西。",
        "  它量的是 approval.rejection_message 那段措辞有没有生效，机制正确性由",
        "  scripts/verify_checkpoint_resume.py 覆盖。approve 那条是对照组：没有它，",
        "  一个永远不敢调写工具的模型在拒绝那条上也是满分。",
        "- 计划遵从率与「计划步数」必须一起读，顺序不能反：步数为 0 时遵从率是 '-'，",
        "  而 0 步有两种完全不同的含义——模型判断这个问题不用分步（正确，见",
        "  prompts/agent_plan/ 里那条「不需要工具就输出 []」），或者规划那次调用",
        "  静默失效了（故障，planner 会打一条 warning）。同一类静默失效在这个仓库",
        "  里已经发生过五次，所以先确认规划真的产出了，再看它有没有被照做。",
        "  遵从率低说明计划是装饰：模型列了步骤然后按自己的想法做。那不一定是坏事",
        "  （计划可能就是错的），要连着关键词命中和轮次一起判断这笔交易值不值。",
        "- 「搜索替身未命中」是模型搜了但罐头数据里没有对应关键词的次数。它不是",
        "  模型的错，而是数据集与替身没对齐；逐题明细里有实际搜索词，照它调。",
        "- 成本列为空表示没配价目表（见 model_prices.example.json），不代表零成本。",
        "- 温度固定 0.0，量的是贪心决策路径。线上默认 0.7，真实表现会比这更抖。",
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 端到端评估")
    parser.add_argument(
        "--variants",
        default="baseline",
        help=f"逗号分隔，或 all。可用：{', '.join(AGENT_VARIANTS)}",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个任务")
    parser.add_argument(
        "--probe",
        default=None,
        help="只跑指定探针，逗号分隔（如 memory_extract,injection）。"
        "在 --limit 之前生效——新加的用例都在文件末尾，--limit 取的是前 N 条，"
        "单独调一组用例时用这个。",
    )
    parser.add_argument("--dataset", default=None, help="改用别的任务集文件")
    parser.add_argument("--out", default=_REPORT_DIR, help="报告输出目录")
    parser.add_argument("--quiet", action="store_true", help="只输出报告，不打进度")
    parser.add_argument(
        "--force",
        action="store_true",
        help="预检查不通过也继续跑（明知数字不可用时才用）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s"
    )

    problems = agent_runner.preflight()
    if problems:
        print("预检查发现问题：")
        for problem in problems:
            print(f"  - {problem}")
        if not args.force:
            # 默认直接退出：这些问题不会让运行报错，只会让报告里的数字安静地失真
            raise SystemExit("修好之后再跑，或者加 --force 明知故犯。")
        print("--force：继续运行，报告里的数字可能不可用。\n")

    variants = resolve(
        [name.strip() for name in args.variants.split(",") if name.strip()]
    )
    tasks = agent_runner.load_tasks(args.limit, path=args.dataset)
    if args.probe:
        wanted = {name.strip() for name in args.probe.split(",") if name.strip()}
        # 先按探针筛，再让 --limit 生效——所以这里用未截断的全集重新加载
        tasks = [
            task
            for task in agent_runner.load_tasks(None, path=args.dataset)
            if task.probe in wanted
        ]
        if args.limit:
            tasks = tasks[: args.limit]
        if not tasks:
            available = sorted(
                {t.probe for t in agent_runner.load_tasks(None, path=args.dataset)}
            )
            raise SystemExit(
                f"没有 probe 匹配 {sorted(wanted)}；可用：{', '.join(available)}"
            )
    if not tasks:
        raise SystemExit("任务集为空")

    # 任务集选定之后才能查"这批任务需要什么"（见 preflight_for 的文档串）
    task_problems = agent_runner.preflight_for(tasks)
    if task_problems:
        print("选中任务的预检查发现问题：")
        for problem in task_problems:
            print(f"  - {problem}")
        if not args.force:
            raise SystemExit("修好之后再跑，或者加 --force 明知故犯。")
        print("--force：继续运行，审批相关的列可能不可用。\n")

    report = await agent_runner.run(variants, tasks)
    markdown = render_markdown(report)

    os.makedirs(args.out, exist_ok=True)
    stamp = app_now().strftime("%Y%m%d-%H%M%S")
    json_path = os.path.join(args.out, f"agent-eval-{stamp}.json")
    md_path = os.path.join(args.out, f"agent-eval-{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(markdown)

    print()
    print(markdown)
    print(f"逐任务明细（含每轮的工具序列与实际搜索词）：{json_path}")


if __name__ == "__main__":
    asyncio.run(main())
