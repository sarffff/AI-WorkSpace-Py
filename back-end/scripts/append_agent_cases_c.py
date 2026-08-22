"""往 agent_tasks.jsonl 追加 C 组用例：跨领域多步任务。

**为什么要这一组。** `delegation-augment` 变体问的是"模型自己会不会在该委派的
时候委派"，但现有用例全是单领域单步——那种题正确的选择基本都是"不委派"，
于是这个变体测不出任何东西（委派率恒为 0，而 0 既可能是"判断正确"也可能是
"根本不会用这个工具"，分不开）。

这 4 条的共同点是**一个正确答案需要两份不同领域的文档**，也就是有意义的分工点。
它们对单代理配置同样是有效用例（跨文档检索本身就更难），所以不会变成
"只有委派变体才跑得通"的题。

关键词都避开了子串陷阱：keyword_coverage 是纯子串匹配（metrics.py:103），
所以不用「5」这种会被「15」命中的写法，改用语料里的专有名词。
"""
from __future__ import annotations

import io
import json
import os

DATASET = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "eval",
    "datasets",
    "agent_tasks.jsonl",
)

RETRIEVAL = ["search_knowledge_base", "read_document_chunk", "list_knowledge_documents"]

CASES: list[dict] = [
    {
        "id": "multi-newbie-prod-access",
        "probe": "multi_domain",
        "title": "跨入职流程与安全规范：新人拿生产写权限",
        "use_rag": True,
        "rubric": (
            "正确答案必须同时用到两份文档。入职指南那边：生产环境跳板机账号需部门负责人"
            "审批，通常 3 个工作日。信息安全规范那边：生产环境默认只读，写权限要走《临时提权"
            "申请》且单次最长 4 小时到期自动回收，所有生产操作经跳板机并全程录屏。"
            "只答出账号开通时效、没提到「默认只读 + 临时提权」这层限制，或者反过来只讲"
            "提权、没提账号本身要审批，都算不完整。"
        ),
        "turns": [
            {
                "question": "我刚入职，想拿到生产环境的写权限，大概要多久、需要走哪些流程？",
                "expect_tools": ["search_knowledge_base"],
                "allow_tools": RETRIEVAL,
                "forbid_tools": ["save_to_knowledge_base", "web_search"],
                "must_include": ["跳板机", "临时提权"],
                "min_rounds": 2,
            }
        ],
    },
    {
        "id": "multi-api-launch-check",
        "probe": "multi_domain",
        "title": "跨 API 规范与安全规范：接口上线前检查",
        "use_rag": True,
        "rubric": (
            "必须覆盖两个领域。API 规范那边至少要提到：写接口用 Idempotency-Key 做幂等"
            "（服务端保留 24 小时）、限流按 access_token 每分钟 600 次、以及 429 要读"
            "Retry-After。安全规范那边至少要提到：密钥由 KMS 托管、代码与配置文件里不得"
            "出现明文密钥、CI 用 OIDC 短期凭据而不是长期 AccessKey。只答一边算失败。"
        ),
        "turns": [
            {
                "question": "我们要上线一个对外的写接口，按公司规定，接口设计和密钥这两块分别有什么要求？",
                "expect_tools": ["search_knowledge_base"],
                "allow_tools": RETRIEVAL,
                "forbid_tools": ["save_to_knowledge_base", "web_search"],
                "must_include": ["Idempotency-Key", "KMS"],
                "min_rounds": 2,
            }
        ],
    },
    {
        "id": "multi-leave-carry-calc",
        "probe": "multi_domain",
        "title": "年假档位 + 结转规则 + 调休有效期",
        "use_rag": True,
        "rubric": (
            "入职 2 年对应年假 10 天（1 至 3 年档）。当年没用完最多结转 5 天到次年 3 月 31 日，"
            "逾期作废且不折现。调休是另一套规则：有效期为生成之日起 90 天，逾期清零——"
            "必须说清调休不能跟年假一起结转到次年。把年假档位答成 12 天或 15 天、"
            "或者把调休也说成能结转 5 天，都算失败。"
        ),
        "turns": [
            {
                "question": "我入职两年了，今年年假还剩几天没用完的话能带到明年吗？我手上还有一些调休，规则一样吗？",
                "expect_tools": ["search_knowledge_base"],
                "allow_tools": RETRIEVAL,
                "forbid_tools": ["save_to_knowledge_base", "web_search"],
                "must_include": ["结转", "90"],
                "min_rounds": 2,
            }
        ],
    },
    {
        "id": "multi-key-leak-response",
        "probe": "multi_domain",
        "title": "跨安全响应与 API 凭据机制：密钥泄露处置",
        "use_rag": True,
        "rubric": (
            "安全规范那边：发现密钥泄露应在 1 小时内完成吊销并轮换，同时提交安全事件报告；"
            "要立即通知 security@acme.dev 并保留现场，不要自行重启或清理日志；涉及 L4 数据"
            "或生产可用性属 P0，需在 15 分钟内拉起应急群。API 指南那边补充了凭据侧的事实："
            "同一账号最多同时持有 5 个有效 refresh_token，超出后最旧的会被吊销。"
            "只讲「赶快换掉密钥」而没有给出时限与上报动作，算失败。"
        ),
        "turns": [
            {
                "question": "我发现有个生产密钥被提交到了公开仓库，现在该做什么？有时间要求吗？",
                "expect_tools": ["search_knowledge_base"],
                "allow_tools": RETRIEVAL,
                "forbid_tools": ["save_to_knowledge_base", "web_search"],
                "must_include": ["轮换", "security@acme.dev"],
                "min_rounds": 2,
            }
        ],
    },
]


def main() -> None:
    with io.open(DATASET, "r", encoding="utf-8") as handle:
        existing = [json.loads(line) for line in handle if line.strip()]
    known = {case["id"] for case in existing}

    fresh = [case for case in CASES if case["id"] not in known]
    if not fresh:
        print("已全部存在，未追加")
        return

    with io.open(DATASET, "a", encoding="utf-8", newline="\n") as handle:
        for case in fresh:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")

    print(f"追加 {len(fresh)} 条：{', '.join(c['id'] for c in fresh)}")
    print(f"数据集现共 {len(existing) + len(fresh)} 条")


if __name__ == "__main__":
    main()
