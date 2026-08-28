/** 遥测/用量相关的通用格式化与标签工具(从 UsagePanel 抽出,供工作台/轨迹页复用) */

/** span 名到中文标签。未登记的原样显示,避免加了新埋点就"消失" */
export const SPAN_LABELS: Record<string, string> = {
  "chat.turn": "整轮回答",
  "llm.chat": "对话生成",
  "llm.summary": "历史摘要",
  "llm.query_rewrite": "查询改写",
  "llm.rerank": "结果重排",
  "llm.judge": "评估裁判",
  "llm.eval_answer": "评估作答",
  "retrieval.hybrid": "混合检索",
  "retrieval.dense": "向量检索",
  "embedding.embed": "文本向量化",
};

export const spanLabel = (name: string) =>
  SPAN_LABELS[name] ??
  (name.startsWith("tool.") ? `工具 · ${name.slice(5)}` : name);

export const fmtInt = (value: number) => value.toLocaleString();

export const fmtMs = (value: number | null) => {
  if (value === null) return "-";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
};

export const fmtCost = (amount: number | null, currency: string | null) => {
  if (amount === null) return "-";
  const symbol = currency === "USD" ? "$" : currency === "CNY" ? "¥" : "";
  return `${symbol}${amount.toFixed(4)}${symbol ? "" : ` ${currency ?? ""}`}`;
};

/** 后端工具名 -> 用户可读标签。未登记的工具名直接原样展示。 */
export const TOOL_LABELS: Record<string, string> = {
  search_knowledge_base: "检索知识库",
  list_knowledge_documents: "查看知识库文档",
  read_document_chunk: "读取文档分块",
};

export const toolLabel = (tool?: string) =>
  (TOOL_LABELS[tool ?? ""] ?? tool) || "工具";
