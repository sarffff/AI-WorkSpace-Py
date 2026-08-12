# 离线评估

回答一个之前无法回答的问题：**改了检索配置，到底变好了还是变坏了。**

阶段二加了十几个开关（混合检索、邻域扩展、查询改写、重排、分块大小），
但没有任何度量手段，等于凭感觉调参。这套 harness 就是那把尺子。

## 快速开始

```bash
cd back-end
python -m eval.run --limit 5                    # 小样本试跑，先确认链路通
python -m eval.run                              # baseline 跑全量
python -m eval.run --variants baseline,dense-only,rerank
python -m eval.run --variants all                # 全部变体对照
```

报告写到 `eval/reports/`：`.md` 是对照表，`.json` 是逐题明细（含裁判理由与完整回答）。

会真实调用模型与 embedding 接口，**产生费用**。开销约为
`问题数 × 变体数 × (1 次生成 + 1 次裁判)`，加上检索本身的 embedding 调用。
先用 `--limit` 估算。

## 组成

| 文件 | 作用 |
| ---- | ---- |
| `corpus/` | 5 篇自造的公司文档（员工手册、报销、API、安全、入职） |
| `datasets/rag_golden.jsonl` | 28 条问答与来源标注 |
| `metrics.py` | recall@k / precision@k / MRR / nDCG@k，纯函数零成本 |
| `judge.py` | LLM-as-judge：忠实度、相关性、拒答 |
| `variants.py` | 配置变体定义 |
| `runner.py` | 执行与汇总 |
| `run.py` | CLI 与报告渲染 |

语料是自造的，不含任何真实数据，因此可以提交进仓库、结果可复现。

## 两个关键设计决定

**相关性标在文档级，不是分块级。** 分块配置一改，chunk id 全变，标在分块上的
金标准立刻作废——而分块大小正是要扫描的变量之一。标在文档上，同一批标注
能复用于任何分块与检索配置。代价是粒度粗：一篇文档里检索到错误章节也算命中。

**评的是 RAG 问答链路，不是完整 Agent 循环。** 检索 + 单次生成，不走多轮工具决策。
Agent 循环的方差主要来自模型每轮的决策，混进来会让「检索改动有没有用」无法归因。
端到端的 Agent 评估应该单独设一套。

## 探针类型

每条问题带 `probe` 标签，报告会按类型拆开看召回率——这样能看出某个开关
到底在帮哪类查询：

- `lexical` — 字面精确匹配（`RESOURCE_EXHAUSTED`、`P8`）。BM25 通道主要提升这一类。
- `paraphrase` — 提问措辞和文档完全不同。稠密向量的主场。
- `table_lookup` — 答案在表格行里，考分块有没有把表切碎。
- `boundary` / `cross_section` — 答案跨越两个段落或小节，考邻域扩展。
- `cross_document` — 需要综合两篇文档。
- `absent` — 知识库里没有。正确行为是拒答，考的是抗幻觉。

## 指标怎么读

- **recall@k** 召回够不够。低了说明检索侧漏了，调重排没用。
- **nDCG@k** 排序好不好。召回一样但 nDCG 高，说明重排起了作用。
- **MRR** 第一个正确结果排多前。
- **忠实度 / 相关性** 1-5 分，LLM 裁判给。**只用于变体间相对比较**，
  不要当绝对质量指标——裁判自己也会错。
- **拒答率** 只统计 `absent` 类问题，越高越好。
- **成本列为空** 表示没配价目表（见 `model_prices.example.json`），不是零成本。

裁判输出解析失败的样本会被剔除并单独计数，不会当 0 分拉低均值。

## 语料隔离

评估语料挂在固定伪用户 `eval-harness` 下，与真实用户数据完全隔离。
文档名带分块配置指纹（如 `hr-handbook.md#a1b2c3d4e5f6`），
换分块配置时旧索引自动删除重建——避免「测的是上一个配置留下的索引」这种静默错误。

## 扩展

28 条是**起点**，不是够用的量。真正能支撑决策的评估集通常要 100 条以上，
且需要覆盖你自己业务里的真实提问。加题目只要往 `rag_golden.jsonl` 追加一行：

```json
{"id": "...", "probe": "lexical", "answerable": true, "question": "...", "expected_documents": ["hr-handbook.md"], "must_include": ["关键数字"], "reference_answer": "..."}
```

加变体则在 `variants.py` 的 `VARIANTS` 里加一项，只改一到两个开关——
一次改一堆参数，跑出差异也说不清是谁的功劳。
