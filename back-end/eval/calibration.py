"""裁判-人工标注一致性校准。

standard.md（评估维度）说得直接："judge 本身需要用人工标注校准，否则只是把一个
不可靠换成了另一个。" 而 judge.py 也自述分数"只用于变体之间的相对比较，不当绝对
质量指标"——校准这一步就是把它从"相对可比"抬到"可信"。

## 做什么、不做什么

**做**：拿一批**人工标注**过的 (问题, 回答) 样本让真实裁判跑一遍，算裁判分与人工分
的一致性——加权 kappa（扣掉瞎猜也能撞上的部分）、Spearman（排序一致性）、MAE 与
精确一致率。指标定义在 eval/metrics.py 的纯函数里。

**不做**：不改裁判 prompt 去"让它更准"。校准是**测量**不是调参——先知道差多少，才谈得上
改哪；改完再跑一次看 kappa 动没动，那才是闭环。

## 诚实边界

- 金标少于 MIN_N 条时报告顶部明确警告"样本不足、勿据此下结论"：几条样本的 kappa
  波动极大。
- 附带的种子集是**无歧义**用例（明显忠实/编造/拒答），与 rag_golden、agent_tasks 一样
  手工标注、非伪造；但它只是让脚本跑得起来的起点，真正校准要拿线上复核样本扩到
  MIN_N 以上。
- 脚本**会真的调模型**（每条一次裁判调用），同变体评估按需手动跑、不进 CI 默认。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from eval import metrics
from eval.judge import AnswerJudge, TaskJudge
from services.model_adapter import OpenAICompatibleAdapter

_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_PATH = os.path.join(_EVAL_DIR, "datasets", "judge_calibration.jsonl")

# 低于这个条数，报告只当"跑通了"，不当校准结论——校准要有统计意义
MIN_N = 20

# 1-5 序数分数维度 vs 布尔维度，两类用不同的一致性指标
_ORDINAL_DIMS = ("faithfulness", "relevance", "success", "grounded")
_BOOL_DIMS = ("abstained", "fabricated_tool_output")

def load_cases(path: str = DATASET_PATH) -> list[dict[str, Any]]:
    """读金标 jsonl。文件不存在返回空列表（报告据此报"未校准"）。

    允许 ``//`` 开头的注释行：种子集里要留几句说明"这是什么、怎么扩"。
    """
    if not os.path.exists(path):
        return []
    cases: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("//"):
                cases.append(json.loads(line))
    return cases

async def run_judges(
    cases: list[dict[str, Any]], *, model: str | None = None
) -> list[dict[str, Any]]:
    """对每条样本跑真实裁判，返回逐维度的 (human, judge) 配对。

    裁判失败（空回答 / 解析不出）的那条整条跳过——把失败当 0 分会污染一致性，
    同 metrics 里对裁判失败的处置。
    """
    adapter = OpenAICompatibleAdapter()
    answer_judge = AnswerJudge(adapter, model)
    task_judge = TaskJudge(adapter, model)

    pairs: list[dict[str, Any]] = []
    for case in cases:
        kind = case.get("kind", "answer")
        human = case.get("human") or {}
        if kind == "task":
            verdict = await task_judge.judge(
                question=case.get("question", ""),
                answer=case.get("answer", ""),
                evidence=case.get("evidence", ""),
                rubric=case.get("rubric", ""),
            )
            judged = {
                "success": verdict.success,
                "grounded": verdict.grounded,
                "fabricated_tool_output": verdict.fabricated_tool_output,
            }
        else:
            answerable = bool(case.get("answerable", True))
            verdict = await answer_judge.judge(
                question=case.get("question", ""),
                answer=case.get("answer", ""),
                context=case.get("context", ""),
                answerable=answerable,
            )
            judged = (
                {"faithfulness": verdict.faithfulness, "relevance": verdict.relevance}
                if answerable
                else {"abstained": verdict.abstained}
            )
        if verdict.failed:
            continue
        for dim, judge_value in judged.items():
            if dim in human and judge_value is not None:
                pairs.append(
                    {"id": case.get("id"), "dim": dim, "human": human[dim], "judge": judge_value}
                )
    return pairs

def _to_number(value: Any, dim: str) -> float:
    """标注值归一成数：布尔维度 True/False → 1/0，序数维度直接取数。"""
    if dim in _BOOL_DIMS:
        return 1.0 if value in (True, 1, "true", "True") else 0.0
    return float(value)


def compute_report(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """按维度汇总一致性。序数维度出 kappa+spearman+MAE+一致率，布尔维度出一致率+kappa。"""
    by_dim: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        by_dim.setdefault(pair["dim"], []).append(pair)

    dims: dict[str, Any] = {}
    for dim, rows in by_dim.items():
        human = [_to_number(r["human"], dim) for r in rows]
        judge = [_to_number(r["judge"], dim) for r in rows]
        entry: dict[str, Any] = {
            "n": len(rows),
            "agreement": metrics.agreement_rate(human, judge),
        }
        if dim in _BOOL_DIMS:
            entry["kappa"] = metrics.cohens_kappa(
                [int(x) for x in human], [int(x) for x in judge], weighted=False
            )
        else:
            entry["mae"] = metrics.mean_absolute_error(human, judge)
            entry["kappaWeighted"] = metrics.cohens_kappa(
                [int(x) for x in human], [int(x) for x in judge], weighted=True
            )
            entry["spearman"] = metrics.spearman(human, judge)
        dims[dim] = entry

    total = len(pairs)
    return {"pairs": total, "underpowered": total < MIN_N, "minN": MIN_N, "dims": dims}

def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _kappa_label(kappa: float | None) -> str:
    """Landis-Koch 粗档，给读者一个量级感（不是硬阈值）。"""
    if kappa is None:
        return ""
    if kappa < 0.2:
        return "slight"
    if kappa < 0.4:
        return "fair"
    if kappa < 0.6:
        return "moderate"
    if kappa < 0.8:
        return "substantial"
    return "almost-perfect"


def render(report: dict[str, Any]) -> str:
    if report["pairs"] == 0:
        return (
            "未校准：金标为空。\n"
            f"往 {DATASET_PATH} 加人工标注样本后重跑（格式见模块文档）。"
        )
    lines: list[str] = []
    if report["underpowered"]:
        lines.append(
            f"⚠ 样本不足（配对 {report['pairs']} < MIN_N {report['minN']}）："
            "下面的数字仅供跑通验证，勿据此下校准结论。"
        )
    lines.append(f"裁判校准报告（配对样本 {report['pairs']}）")
    lines.append(
        f"{'维度':<20}{'n':>4}{'一致率':>8}{'MAE':>8}{'kappa':>8}  {'档':<14}{'spearman':>9}"
    )
    for dim, entry in report["dims"].items():
        kappa = entry.get("kappaWeighted", entry.get("kappa"))
        lines.append(
            f"{dim:<20}{entry['n']:>4}{_fmt(entry['agreement']):>8}"
            f"{_fmt(entry.get('mae')):>8}{_fmt(kappa):>8}  {_kappa_label(kappa):<14}"
            f"{_fmt(entry.get('spearman')):>9}"
        )
    return "\n".join(lines)

async def _amain(args: argparse.Namespace) -> None:
    cases = load_cases(args.dataset)
    if args.dry_run:
        print(f"[dry-run] 载入 {len(cases)} 条金标，未调用模型。")
        for case in cases:
            head = str(case.get("question", ""))[:40]
            print(f"  {case.get('id')}  [{case.get('kind', 'answer')}]  {head}")
        return
    pairs = await run_judges(cases, model=args.model)
    report = compute_report(pairs)
    print(render(report))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"\n报告 JSON 已写入 {args.json}")


def main() -> None:
    parser = argparse.ArgumentParser(description="裁判-人工标注一致性校准")
    parser.add_argument("--dataset", default=DATASET_PATH, help="金标 jsonl 路径")
    parser.add_argument(
        "--model", default=None, help="裁判模型（默认走 settings.judge_model）"
    )
    parser.add_argument("--json", default=None, help="把报告写成 JSON 到此路径")
    parser.add_argument(
        "--dry-run", action="store_true", help="只列样本，不调用模型"
    )
    asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    main()
