# -*- coding: utf-8 -*-
"""给四条 multi_domain 用例补第二轮，让它们真的考编排而不只是跨文档综合。

## 改之前它们量的是什么

四条全是单轮，其中三条 `expect_tools=[]` + `min_rounds=1`。而 `RAG_PREFETCH=true`
会在 round 0 就把资料取回来注入上下文，于是模型一轮作答、零次工具调用就能拿满分。
也就是说这个探针实际量的是**"给定已经摆在眼前的多篇资料，能不能综合"**——那是个
有价值的能力，但它不是编排，跟 `delegation-augment` 想证明的东西没有关系：
一次预检索、零次自主调用，主代理根本没有可委派的东西。

`multi-newbie-prod-access` 是唯一标了 `min_rounds=2` 的，所以四条里只有它给了
"再查一次"的空间。

## 改成什么

每条补一个**追问**轮，要求同时满足两件事：

1. **两处事实分属不同文档的不同小节**，而 must_include 的两个词各来自一边。
   一个词命中不了就能看出是哪一边没查到，而不是只知道"答得不全"。
2. **是追问口吻**（"那…"、"这次…"、"我们…"）。这顺带把指代消解
   （RAG_CONDENSE_QUERY）拉进覆盖范围——那条路 2026-08-22 之前一直是死的
   （max_tokens=256 被思考吃光，返回空串，静默退回原文），所以此前没有任何
   多轮用例真的走过它。

## 第一版的设计假设是错的，实测打回来了

原本还想让第二轮**逼出一次自主检索**：给 `expect_tools=["search_knowledge_base"]`
和 `min_rounds=2`，理由是"一次预检索只对一个查询做，覆盖不了两篇文档"。

实测四条 turn-2 全是 `rounds=1`、`modelToolCalls=0`、`toolRecall=0.0`：语料只有
6 篇、hybrid + top_k=5 + 邻域扩展，而指代消解后的追问同时提到了两个主题，于是
预检索**真的把两篇文档的分块都拿回来了**。问题问得再难也逼不出第二次检索。

那个 `expect_tools` 于是变成一个恒为 0 的召回——**和本轮早先修掉的 8 条用例
一模一样的错**（它们把 `search_knowledge_base` 标成 expect，而 RAG_PREFETCH 让它
在 round 0 就跑完了）。刚修完又在新用例里犯了一次，所以这条记在这里：
**RAG_PREFETCH 开着的时候，"该不该检索"就不再是模型的决策，不能拿它当断言。**

## 真正的判别信号是答案完整度，不是工具次数

而实测同时给出了一个更好的信号：8 轮里 6 轮 keyword_coverage 满分，两轮 0.5——
`multi-leave-carry-calc` 答了临时提权那半、漏了年假提前几天；
`multi-key-leak-response` 答了 OIDC 那半、漏了 Retry-After。

也就是说模型拿预检索给到的东西作答，**缺的那半不会自己回头去找**。这正是委派
可能补上的地方（派一个子代理专门去查 API 那一侧），而且它现在是个有余量的数
（0.5，不是 1.0），所以 `delegation-augment` 的收益有地方体现。

`min_rounds` 保持 1：baseline 一轮就能拿到 0.5 覆盖，那就是"最省"的基线。委派
变体如果靠多花两轮把覆盖做到 1.0，报告上会呈现为轮次效率下降、关键词命中上升
——那正是这笔交易该有的样子，把成本藏起来才是错的。

## 关键词都避开子串陷阱

`keyword_coverage` 是纯子串匹配，没有空白归一化（eval/metrics.py）。所以这里一律
用 `429` / `180` / `OIDC` / `Retry-After` / `跳板机` / `1:1.5` 这类唯一串，不用裸数字：
`60` 会命中 `600`，`15` 会命中 `150`，而带空格的 `3 个工作日` 会被模型写成
`3个工作日` 而判成没命中。这两个坑本轮都真的踩过。

## 不动第一轮

第一轮原样保留。它量的跨文档综合是真实能力，改掉就丢了一条已经跑出过基线的
用例；补一轮是加信息，改一轮是换标的。
"""

from __future__ import annotations

import json
from pathlib import Path

DATASET = Path(__file__).resolve().parents[1] / "eval" / "datasets" / "agent_tasks.jsonl"

_ALLOW = [
    "search_knowledge_base",
    "read_document_chunk",
    "list_knowledge_documents",
    # 委派不算噪声——理由见 scripts/fix_allow_delegate.py
    "delegate",
]

# case_id -> 追加的第二轮
FOLLOW_UPS: dict[str, dict] = {
    "multi-newbie-prod-access": {
        "question": (
            "那这次提权到期之后，如果我还要在休息日继续跟这个问题，"
            "写权限得重新申请吗？那天的加班怎么折算，换来的调休能留多久？"
        ),
        # 安全规范《生产环境访问》：单次最长 4 小时、到期自动回收
        # 员工手册《加班与调休》：休息日 1:1.5、调休 90 天
        # 不标 expect_tools：RAG_PREFETCH 开着时"该不该检索"不是模型的决策，
        # 标了就是一个恒为 0 的召回（见模块文档"第一版的设计假设是错的"）
        "allow_tools": _ALLOW,
        "must_include": ["1:1.5", "90"],
        "min_rounds": 1,
    },
    "multi-api-launch-check": {
        "question": (
            "这个接口上线以后要是被调用方刷爆了，限流是怎么反馈给对方的？"
            "另外我们在生产上改配置这件事，有没有审计要求？"
        ),
        # API 指南《限流》：429 + Retry-After
        # 安全规范《生产环境访问》：跳板机全程录屏、录像保留 180 天
        # 不标 expect_tools：RAG_PREFETCH 开着时"该不该检索"不是模型的决策，
        # 标了就是一个恒为 0 的召回（见模块文档"第一版的设计假设是错的"）
        "allow_tools": _ALLOW,
        "must_include": ["429", "180"],
        "min_rounds": 1,
    },
    "multi-leave-carry-calc": {
        "question": (
            "我打算年底连着请几天。年假要提前多久在 OA 提？"
            "还有那段时间我可能得轮值处理生产问题，写权限那边要怎么走？"
        ),
        # 员工手册《年假》：提前 3 个工作日提交
        # 安全规范《生产环境访问》：临时提权 + 跳板机
        # 不标 expect_tools：RAG_PREFETCH 开着时"该不该检索"不是模型的决策，
        # 标了就是一个恒为 0 的召回（见模块文档"第一版的设计假设是错的"）
        "allow_tools": _ALLOW,
        "must_include": ["工作日", "跳板机"],
        "min_rounds": 1,
    },
    "multi-key-leak-response": {
        "question": (
            "泄露的这个密钥是 CI 在用的长期 AccessKey，按规定本来该配成什么？"
            "我们轮换之后调用方会不会大面积失败，接口那边有什么机制让他们知道该重试？"
        ),
        # 安全规范《密钥管理》：CI 走 OIDC 短期凭据，禁止长期 AccessKey
        # API 指南《限流》：Retry-After 给出建议等待秒数
        # 不标 expect_tools：RAG_PREFETCH 开着时"该不该检索"不是模型的决策，
        # 标了就是一个恒为 0 的召回（见模块文档"第一版的设计假设是错的"）
        "allow_tools": _ALLOW,
        "must_include": ["OIDC", "Retry-After"],
        "min_rounds": 1,
    },
}


def main() -> None:
    lines = DATASET.read_text(encoding="utf-8").splitlines()
    cases = [json.loads(line) for line in lines if line.strip()]

    added: list[str] = []
    replaced: list[str] = []
    for case in cases:
        follow_up = FOLLOW_UPS.get(case["id"])
        if follow_up is None:
            continue
        turns = case["turns"]
        if len(turns) >= 2:
            # 覆盖而不是再追加一轮：重跑这个脚本不该让用例越长越长。
            # 判据是"第二轮的问题和这里写的不一样"，这样改断言时只改这个文件。
            if turns[1] == follow_up:
                continue
            turns[1:] = [follow_up]
            replaced.append(case["id"])
        else:
            turns.append(follow_up)
            added.append(case["id"])

    if not added and not replaced:
        print("没有需要改的，已是目标状态。")
        return

    DATASET.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n",
        encoding="utf-8",
    )
    if added:
        print(f"补第二轮 {len(added)} 条：{', '.join(added)}")
    if replaced:
        print(f"替换第二轮 {len(replaced)} 条：{', '.join(replaced)}")


if __name__ == "__main__":
    main()
