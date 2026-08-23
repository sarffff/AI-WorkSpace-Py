"""给 rag_golden.jsonl 追加第二批题（配合语料从 6 篇扩到 13 篇）。

**为什么要这一批。** 扩容前 `recall@5` 在 15 个变体 21 轮里只有两个取值
（`dense-only` 0.9643，其余全 1.0）。6 篇 40 分块下混合检索已经做满，于是所有
召回侧技术——多查询、HyDE、查询路由、top-k、分块大小——全部测不出差别。这不是
尺子坏了，是题太简单。语料现在 13 篇 92 分块，需要配套的题才能把新的召回压力
变成数字。

**三类新探针**：

- ``near_miss``：答案在 A 文档，但 B 文档有一段词汇高度相似却答不了。考的是
  RRF 之后的排序，也是 cross-encoder 唯一能显形的地方。
- ``conflict``：两篇文档给出不同规定，且语料里**明写了**谁优先。考的是模型会不
  会照抄先检索到的那条。
- ``cross_document``：必须同时命中两篇才能答全。

**must_include 的两个坑**（都踩过）：

1. 纯子串匹配，无空白归一化。``"3 个工作日"`` 匹配不上 ``"3个工作日"``。所以
   关键词优先选**不含空格的词**，而不是带量词的短语。
2. 数字会互相包含。``"5"`` 命中 ``"15"``，``"60"`` 命中 ``"600"``。所以问国内
   提前期时不能用 ``"5"``——同篇文档里 ``"15 个工作日"`` 会让错答案也算过。
   校验器会主动查这一类，见 ``_check_digit_collision``。

用法：

    python scripts/append_golden_wave2.py --dry-run   # 只校验，不写
    python scripts/append_golden_wave2.py             # 校验通过才追加
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
GOLDEN = os.path.join(_BACKEND, "eval", "datasets", "rag_golden.jsonl")
CORPUS = os.path.join(_BACKEND, "eval", "corpus")

# 已有的 probe 取值，加上这一批引入的三类。校验器拿它挡拼写错误——probe 写错不会
# 报错，只会让分组统计里多出一个只有一条题的桶。
KNOWN_PROBES = {
    "paraphrase",
    "table_lookup",
    "lexical",
    "boundary",
    "cross_document",
    "cross_section",
    "absent",
    "injection",
    "near_miss",
    "conflict",
}


def _norm(text: str) -> str:
    """去掉所有空白。语料里写 ``5 个工作日``，模型可能答 ``5个工作日``。

    只在**校验**时用：确认关键词确实在语料里存在。运行期的
    ``metrics.keyword_coverage`` 是纯子串匹配，不做归一化——这里归一化是为了不把
    "语料里没这句" 和 "空格对不上" 混成同一个错误。
    """
    return re.sub(r"\s+", "", text)


def _load_corpus() -> dict[str, str]:
    return {
        name: open(os.path.join(CORPUS, name), encoding="utf-8").read()
        for name in os.listdir(CORPUS)
        if name.endswith(".md")
    }


# ---------------------------------------------------------------- 新题

CASES: list[dict] = [
    # ── 采购 vs 报销：金额分档审批是两篇共有的结构，词汇几乎一样 ──
    {
        "id": "procure-approval-800",
        "probe": "near_miss",
        "answerable": True,
        "question": "买一批 800 元的办公椅，需要谁批？",
        "expected_documents": ["procurement-policy.md"],
        # 不用 "1000"：报销那篇也有金额档。用"行政负责人"——只有采购篇有这个角色，
        # 它同时证明命中了对的文档、又证明落在了对的档位（1000 以下只需直属主管）
        "must_include": ["直属主管"],
        "reference_answer": "800 元属于单笔 1000 元以下，只需直属主管审批。注意这走的是采购流程而非报销：采购须事前提交《采购申请单》，未经审批的先行采购财务有权拒付。",
    },
    {
        "id": "procure-tender-threshold",
        "probe": "near_miss",
        "answerable": True,
        "question": "六万块的设备采购要走招标吗？",
        "expected_documents": ["procurement-policy.md"],
        # "招标"只在采购篇出现，是区分两篇的干净锚点
        "must_include": ["招标"],
        "reference_answer": "需要。单笔 50000 元以上除追加 CEO 审批外还要进入招标流程。",
    },
    {
        "id": "procure-quote-count",
        "probe": "lexical",
        "answerable": True,
        "question": "采购比价要几家报价？有免的情况吗？",
        "expected_documents": ["procurement-policy.md"],
        # 故意不断言数字 3："3" 会被同篇的 "30 天" 误命中，而 "3 家" 又依赖空格
        # （纯子串匹配不归一化空白）。这是 keyword_coverage 的固有限制，选词绕不过
        # 去，只能承认：这条题验的是"提到了比价家数下限与豁免规则"，不验具体数字
        "must_include": ["不少于", "豁免"],
        "reference_answer": "单笔 10000 元以上须取得不少于 3 家报价并留档；连续两年合作的供应商可豁免比价，但每年需重新评估资质。",
    },
    # ── 差旅预订 vs 费用报销：语料里明写了冲突时以报销制度为准 ──
    {
        "id": "travel-overseas-lead-time",
        "probe": "paraphrase",
        "answerable": True,
        "question": "出国出差要提前多久申请？",
        "expected_documents": ["travel-booking.md"],
        # 不用 "15" 单独出现——同篇有 "5 个工作日"，而 "5" 是 "15" 的子串，
        # 反向会误判。配上"签证"锁定境外那一条
        "must_include": ["15", "签证"],
        "reference_answer": "境外行程须提前 15 个工作日提交，因涉及签证与保险。",
    },
    {
        "id": "travel-urgent-domestic",
        "probe": "boundary",
        "answerable": True,
        "question": "后天就要出差，来不及走 5 个工作日的流程怎么办？",
        "expected_documents": ["travel-booking.md"],
        "must_include": ["特批"],
        "reference_answer": "3 个工作日以内的紧急行程需部门负责人在系统内特批。",
    },
    {
        "id": "travel-hotel-conflict",
        "probe": "conflict",
        "answerable": True,
        "question": "住宿标准差旅预订指引和报销制度写得不一样，该按哪个执行？",
        "expected_documents": ["travel-booking.md", "expense-policy.md"],
        "must_include": ["费用报销制度"],
        "reference_answer": "以《费用报销制度》为准——《差旅预订指引》明确说明两者冲突时报销制度优先。预订指引管怎么订（协议酒店、超出协议价自理），报销制度管能报多少（一线 600 元、二线 450 元、其他 350 元）。",
    },
    # ── IT 支持 vs 入职/安全 ──
    {
        "id": "it-p1-response",
        "probe": "table_lookup",
        "answerable": True,
        "question": "生产挂了，IT 多久会响应？",
        "expected_documents": ["it-support.md"],
        "must_include": ["15"],
        "reference_answer": "P1（生产不可用）首次响应 15 分钟，解决目标 4 小时。仅提工单不视为已上报，须同时电话联系值班工程师。",
    },
    {
        "id": "it-password-lockout",
        "probe": "lexical",
        "answerable": True,
        "question": "密码输错几次会被锁？锁多久？",
        "expected_documents": ["it-support.md"],
        "must_include": ["30", "锁定"],
        "reference_answer": "连续 5 次失败会锁定 30 分钟，锁定期内不受理加急重置。",
    },
    # ── 绩效 ──
    {
        "id": "perf-appeal-window",
        "probe": "paraphrase",
        "answerable": True,
        "question": "对绩效结果不服，还能申诉吗？找谁？",
        "expected_documents": ["performance-review.md"],
        "must_include": ["10", "HRBP"],
        "reference_answer": "可在结果沟通后 10 个工作日内向 HRBP 提交书面申诉，由 HRBP 与被申诉主管的上一级共同复核，复核结论为最终结论。",
    },
    {
        "id": "perf-pip-length",
        "probe": "paraphrase",
        "answerable": True,
        "question": "拿了 D 之后会怎样？",
        "expected_documents": ["performance-review.md"],
        "must_include": ["绩效改进", "3"],
        "reference_answer": "出现 D 的进入绩效改进计划（PIP），周期 3 个月，由主管与 HRBP 共同制定可衡量目标；期满未达标公司可依法解除劳动合同，期间不得申请内部转岗。",
    },
    # ── 远程办公：跨文档，且与安全规范硬冲突 ──
    {
        "id": "remote-monthly-cap",
        "probe": "paraphrase",
        "answerable": True,
        "question": "临时在家办公每个月最多几天？要审批吗？",
        "expected_documents": ["remote-work.md"],
        "must_include": ["报备"],
        "reference_answer": "每月临时远程累计不超过 5 天；当日 10:00 前在系统报备即可，无需审批。",
    },
    {
        "id": "remote-prod-access",
        "probe": "cross_document",
        "answerable": True,
        "question": "在家能连生产环境改配置吗？",
        "expected_documents": ["remote-work.md", "security-policy.md"],
        "must_include": ["不得", "生产环境"],
        "reference_answer": "不行。远程访问办公网须走 VPN，但生产环境访问在任何情况下都不得从远程发起。",
    },
    # ── 数据留存 ──
    {
        "id": "retention-interview-record",
        "probe": "table_lookup",
        "answerable": True,
        "question": "面试记录要留多久？",
        "expected_documents": ["data-retention.md"],
        "must_include": ["12"],
        "reference_answer": "面试记录留存 12 个月，到期自动删除。",
    },
    {
        "id": "retention-litigation-hold",
        "probe": "paraphrase",
        "answerable": True,
        "question": "打官司的时候，到期该删的数据还能删吗？",
        "expected_documents": ["data-retention.md"],
        "must_include": ["延长", "禁止"],
        "reference_answer": "不能。进入法律诉讼或监管调查的数据留存期自动延长至程序结束，期间禁止任何销毁操作，并须由法务在系统内挂起对应的自动删除任务。",
    },
    # ── 故障复盘 vs IT 支持：P1/P2 是两篇共有的词 ──
    {
        "id": "postmortem-p2-trigger",
        "probe": "near_miss",
        "answerable": True,
        "question": "P2 故障一定要写复盘吗？",
        "expected_documents": ["incident-postmortem.md"],
        "must_include": ["30"],
        "reference_answer": "不一定。P2 只有影响超过 30 分钟才须复盘；P1 无论持续时长都要。另外同一根因 30 天内重复出现、或客户正式投诉可用性问题也须复盘。",
    },
    {
        "id": "postmortem-blame-free",
        "probe": "paraphrase",
        "answerable": True,
        "question": "复盘文档里能写谁操作失误吗？",
        "expected_documents": ["incident-postmortem.md"],
        "must_include": ["根因"],
        "reference_answer": "不能。复盘只追根因不追个人，禁止「某某操作失误」这类表述，应写成「当前流程允许未经二次确认即执行删除」。把原因写成个人问题会让下一次故障被隐瞒。",
    },
]


# ---------------------------------------------------------------- 校验

def _check_digit_collision(keyword: str, docs: list[str], corpus: dict[str, str]) -> str | None:
    """纯数字关键词是否会被同篇文档里更长的数字误命中。

    ``keyword_coverage`` 是纯子串匹配，所以 ``"5"`` 会在 ``"15 个工作日"`` 里命中。
    后果是**错答案也算过**——模型答了境外的 15 天，问的却是国内的 5 天，覆盖率
    仍然 1.0。这类假绿灯比红灯危险，所以新题一律硬失败。
    """
    if not keyword.isdigit():
        return None
    for name in docs:
        for found in re.findall(r"\d+", corpus.get(name, "")):
            if found != keyword and keyword in found:
                return f"数字 {keyword!r} 是 {name} 里 {found!r} 的子串，会被误命中"
    return None


def validate(cases: list[dict], corpus: dict[str, str], existing: list[dict]) -> list[str]:
    errors: list[str] = []
    seen_ids = {row["id"] for row in existing}
    batch_ids: set[str] = set()

    for case in cases:
        cid = case.get("id", "<no id>")
        if cid in seen_ids:
            errors.append(f"{cid}: id 与既有金标冲突")
        if cid in batch_ids:
            errors.append(f"{cid}: id 在本批内重复")
        batch_ids.add(cid)

        if case.get("probe") not in KNOWN_PROBES:
            errors.append(f"{cid}: probe {case.get('probe')!r} 不在已知取值里")

        docs = case.get("expected_documents", [])
        for name in docs:
            if name not in corpus:
                errors.append(f"{cid}: expected_documents 指向不存在的 {name}")

        if case.get("answerable") and not docs:
            errors.append(f"{cid}: answerable=True 但没有 expected_documents")

        for keyword in case.get("must_include", []):
            # 关键词必须在某一篇 expected_documents 里真实出现，否则这条题的
            # keywordCoverage 恒为 0——尺子坏了而报告只会显示"模型答不出来"
            if not any(_norm(keyword) in _norm(corpus.get(n, "")) for n in docs):
                errors.append(f"{cid}: must_include {keyword!r} 在 expected_documents 里找不到")
            collision = _check_digit_collision(keyword, docs, corpus)
            if collision:
                errors.append(f"{cid}: {collision}")

        if not case.get("reference_answer", "").strip():
            errors.append(f"{cid}: reference_answer 为空")
        # 中英混杂：手写时容易把 see/first 之类漏在中文里
        for field in ("question", "reference_answer"):
            for word in re.findall(r"[A-Za-z]{3,}", case.get(field, "")):
                if word not in {"CEO", "HRBP", "PIP", "VPN", "API", "Wi", "Fi", "show"}:
                    errors.append(f"{cid}: {field} 里有可疑英文单词 {word!r}")
    return errors


def audit_existing(existing: list[dict], corpus: dict[str, str]) -> list[str]:
    """对既有 30 条只**告警**不失败。

    追溯改它们会动到历史 keywordCoverage，让新旧报告不可比——那正是这一轮在避免
    的坑。所以这里只把问题列出来，供以后单独处理。
    """
    warnings: list[str] = []
    for row in existing:
        for keyword in row.get("must_include", []):
            hit = _check_digit_collision(keyword, row.get("expected_documents", []), corpus)
            if hit:
                warnings.append(f"{row['id']}: {hit}")
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只校验，不写文件")
    args = parser.parse_args()

    corpus = _load_corpus()
    existing = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]

    print(f"语料 {len(corpus)} 篇，既有金标 {len(existing)} 条，本批新增 {len(CASES)} 条\n")

    warnings = audit_existing(existing, corpus)
    if warnings:
        print(f"既有金标的数字子串隐患（仅告警，不阻塞）：")
        for w in warnings:
            print(f"  ! {w}")
        print()

    errors = validate(CASES, corpus, existing)
    if errors:
        print(f"校验失败 {len(errors)} 项：")
        for e in errors:
            print(f"  x {e}")
        return 1

    print("校验通过。")
    if args.dry_run:
        print("--dry-run，未写入。")
        return 0

    with open(GOLDEN, "a", encoding="utf-8") as handle:
        for case in CASES:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"已追加 {len(CASES)} 条 → {len(existing) + len(CASES)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
