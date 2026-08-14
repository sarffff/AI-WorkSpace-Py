"""评估 CLI。

    python -m eval.run                                  # 只跑 baseline
    python -m eval.run --variants all                   # 全部变体对照
    python -m eval.run --variants baseline,dense-only    # 指定对照
    python -m eval.run --limit 5                         # 先小样本试跑

会真实调用模型与 embedding 接口，产生费用。--variants all 的开销约等于
问题数 x 变体数 次生成 + 同样次数的裁判调用，先用 --limit 估一下再放开。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

from eval import runner
from eval.variants import VARIANTS, resolve
from services.clock import now as app_now

_REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

# 报告表格里展示的列。None 值渲染成 "-"，不要粉饰成 0
_COLUMNS = [
    ("variant", "变体"),
    ("answerPrompt", "提示词"),
    ("recall", "recall@k"),
    ("ndcg", "nDCG@k"),
    ("mrr", "MRR"),
    ("faithfulness", "忠实度"),
    ("relevance", "相关性"),
    ("abstentionRate", "拒答率"),
    ("injectionResistRate", "抗注入率"),
    ("promptTokens", "输入 token"),
    ("completionTokens", "输出 token"),
    ("cost", "成本"),
    ("avgLatencyMs", "平均耗时 ms"),
]


def _pick(summary: dict[str, Any], key: str) -> Any:
    """recall/ndcg 的键带 top_k 后缀，这里按前缀取值。"""
    if key in ("recall", "ndcg"):
        match = next((k for k in summary if k.startswith(f"{key}@")), None)
        return summary.get(match) if match else None
    return summary.get(key)


def _format(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 100 else f"{value:.0f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    summaries = report["summaries"]
    lines = [
        "# RAG 配置对照报告",
        "",
        f"生成时间：{app_now().isoformat(timespec='seconds')}",
        f"问题数：{summaries[0]['questions'] if summaries else 0}",
        "",
        "| " + " | ".join(label for _key, label in _COLUMNS) + " |",
        "| " + " | ".join("---" for _ in _COLUMNS) + " |",
    ]
    for summary in summaries:
        cells = [_format(_pick(summary, key)) for key, _label in _COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")

    lines += ["", "## 按探针类型的召回", ""]
    probes = sorted({p for s in summaries for p in s.get("recallByProbe", {})})
    if probes:
        lines.append("| 变体 | " + " | ".join(probes) + " |")
        lines.append("| " + " | ".join("---" for _ in range(len(probes) + 1)) + " |")
        for summary in summaries:
            by_probe = summary.get("recallByProbe", {})
            cells = [_format(by_probe.get(probe)) for probe in probes]
            lines.append(f"| {summary['variant']} | " + " | ".join(cells) + " |")

    judge_failures = sum(s.get("judgeFailures", 0) for s in summaries)
    if judge_failures:
        lines += [
            "",
            f"> 裁判解析失败 {judge_failures} 次，这些样本已从忠实度/相关性均值中剔除。",
        ]
    lines += [
        "",
        "## 读法",
        "",
        "- 成本列为空表示没配价目表（见 model_prices.example.json），不代表零成本。",
        "- 忠实度/相关性是 LLM 裁判打分，只用于变体间相对比较，不是绝对质量。",
        "- 探针类型里 `lexical` 考字面精确匹配，`paraphrase` 考语义改写，",
        "  `cross_section` / `cross_document` 考跨段落与跨文档，`absent` 考拒答，",
        "  `injection` 考资料夹带指令时会不会被带走。",
        "- 抗注入率只统计带 must_avoid 的样本；它衡量的是「提示词 + 护栏」的联合表现，",
        "  单独下降不能断定是哪一侧退化，要回到 trace 里看 guardrail.* 属性有没有命中。",
        "- 提示词列是本轮用的 eval_rag_answer 版本（正文见 prompts/eval_rag_answer/）。",
        "  只换提示词的变体，检索指标应当与 baseline 逐位相同；不同就说明配置串了。",
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 配置变体评估")
    parser.add_argument(
        "--variants",
        default="baseline",
        help=f"逗号分隔，或 all。可用：{', '.join(VARIANTS)}",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个问题")
    parser.add_argument(
        "--dataset",
        default=None,
        help="改用别的数据集文件（如 eval/datasets/feedback_regression.jsonl）",
    )
    parser.add_argument("--out", default=_REPORT_DIR, help="报告输出目录")
    parser.add_argument("--quiet", action="store_true", help="只输出报告，不打进度")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )

    variants = resolve([name.strip() for name in args.variants.split(",") if name.strip()])
    cases = runner.load_cases(args.limit, dataset_path=args.dataset)
    if not cases:
        raise SystemExit("金标准集为空")

    report = await runner.run(variants, cases)
    markdown = render_markdown(report)

    os.makedirs(args.out, exist_ok=True)
    stamp = app_now().strftime("%Y%m%d-%H%M%S")
    json_path = os.path.join(args.out, f"eval-{stamp}.json")
    md_path = os.path.join(args.out, f"eval-{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(markdown)

    print()
    print(markdown)
    print(f"逐题明细：{json_path}")


if __name__ == "__main__":
    asyncio.run(main())
