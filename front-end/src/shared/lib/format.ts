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

/**
 * 文件大小。null 显示成空字符串而不是 "-"：目录本来就没有大小，
 * 在文件树里摆一个 "-" 会让人以为是取失败了。
 */
export const fmtBytes = (value: number | null) => {
  if (value === null) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
};

export const fmtCost = (amount: number | null, currency: string | null) => {
  if (amount === null) return "-";
  const symbol = currency === "USD" ? "$" : currency === "CNY" ? "¥" : "";
  return `${symbol}${amount.toFixed(4)}${symbol ? "" : ` ${currency ?? ""}`}`;
};

/**
 * 后端工具名 -> 用户可读标签。未登记的工具名直接原样展示。
 *
 * 这张表要跟着后端的工具面走。漏登记不会报错，只是在工具轨迹和审批卡片上显示
 * 原始英文名——而审批卡片恰好是最需要说人话的地方：用户要在那里判断"同不同意"，
 * 标题写着 `delete_knowledge_document` 帮不上忙。
 */
export const TOOL_LABELS: Record<string, string> = {
  search_knowledge_base: "检索知识库",
  list_knowledge_documents: "查看知识库文档",
  read_document_chunk: "读取文档分块",
  calculate: "计算",
  web_search: "搜索网页",
  fetch_web_page: "抓取网页",
  read_attachment: "读取附件",
  save_to_knowledge_base: "写入知识库",
  delete_knowledge_document: "删除知识库文档",
  ask_user: "询问用户",
  delegate: "委派子代理",
  list_directory: "列出文件夹",
  read_file: "读取文件",
  search_files: "搜索文件内容",
  write_file: "写入文件",
  edit_file: "修改文件",
  delete_file: "删除文件",
  load_skill: "加载作业指导",
  read_skill_file: "读取指导附件",
};

export const toolLabel = (tool?: string) =>
  (TOOL_LABELS[tool ?? ""] ?? tool) || "工具";

/**
 * 相对时间（“刚刚 / N 分钟前 / N 小时前 / N 天前”），约一周以上回落到日期。
 *
 * 直接 `new Date(iso)` 解析——业务时间戳是 naive 本地墙上时间（见后端 clock.py），
 * 与浏览器本地时区对齐时准确；这与 app 其余地方（会话列表 / 轨迹页）处理时间的方式
 * 一致。空值 / 不可解析返回空串；细小的时钟差导致的"未来"一律按"刚刚"处理。
 */
export const fmtRelativeTime = (iso: string | null | undefined): string => {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const sec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (sec < 60) return "刚刚";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day} 天前`;
  return new Date(iso).toLocaleDateString();
};
