import type {
  TicketChannel,
  TicketRisk,
  TicketStatus,
  ToolTier,
} from "@/shared/types/api.types";

/**
 * 工单台的标签、语气与格式化。
 *
 * 这些映射放在一处而不是散在各组件里，理由很实际：**它们决定坐席对状态的读法**。
 * 同一个 `awaiting_approval` 在队列里叫"等你批"、在详情里叫"挂起中"、在指标卡上
 * 叫"人工介入"——三个词指同一件事，却会让人以为是三件事。要改名只改这一处。
 */

/**
 * 一个字的"没有数"。后端在样本不足、价目表没命中、遥测关着这些情形下回 null，
 * 而 null 和 0 在业务上是两件事：错误操作率显示 0% 会被读成"零错误"，
 * 它真实的意思是"还没有足够的样本来回答这个问题"。
 */
export const UNKNOWN = "未知";

export const fmtInt = (value: number | null | undefined) =>
  value === null || value === undefined ? UNKNOWN : value.toLocaleString();

export const fmtMs = (value: number | null | undefined) => {
  if (value === null || value === undefined) return UNKNOWN;
  return value >= 1000
    ? `${(value / 1000).toFixed(1)}s`
    : `${Math.round(value)}ms`;
};

/** 0 是有效值，所以只有 null 走"未知"，不做 falsy 判断 */
export const fmtBytes = (value: number | null) => {
  if (value === null) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
};

/** 比率。后端给的是 0..1 的小数或 null */
export const fmtRate = (value: number | null | undefined, digits = 1) =>
  value === null || value === undefined
    ? UNKNOWN
    : `${(value * 100).toFixed(digits)}%`;

/**
 * 金额。后端金额一律字符串（Decimal 序列化），不要转成 float 再格式化：
 * JSON 数字走 IEEE754，在钱的算术上迟早咬人——这条规矩从请求体一路到界面。
 */
export const fmtMoney = (
  value: string | number | null | undefined,
  currency: string | null = "CNY"
) => {
  if (value === null || value === undefined || value === "") return UNKNOWN;
  const text = String(value);
  const symbol =
    currency === "USD" ? "$" : currency === "CNY" || !currency ? "¥" : "";
  return `${symbol}${text}${symbol ? "" : ` ${currency ?? ""}`}`;
};

export const fmtCost = (amount: number | null, currency: string | null) => {
  if (amount === null) return UNKNOWN;
  const symbol = currency === "USD" ? "$" : currency === "CNY" ? "¥" : "";
  return `${symbol}${amount.toFixed(4)}${symbol ? "" : ` ${currency ?? ""}`}`;
};

export const fmtDateTime = (iso: string | null | undefined) => {
  if (!iso) return UNKNOWN;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return UNKNOWN;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
};

/**
 * 相对时间（"刚刚 / N 分钟前 / N 小时前 / N 天前"），一周以上回落到日期。
 *
 * 业务时间戳是 naive 本地墙上时间（后端 clock.py），与浏览器时区一致时准确。
 * 空值与解析失败返回空串——它们在界面上是"还没有这件事"，不是"出错了"。
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
  return new Date(iso).toLocaleDateString("zh-CN");
};

// ========== 工单状态 ==========

export const STATUS_LABELS: Record<string, string> = {
  new: "待受理",
  understanding: "理解中",
  planning: "规划中",
  acting: "办理中",
  awaiting_approval: "等你批",
  escalated: "已转人工",
  resolved: "已解决",
  closed: "已关闭",
  failed: "执行失败",
};

/**
 * 语气档。队列扫一眼要能分辨"这张单在等谁"，所以分成五组：
 * 没人管 / Agent 在办 / 人在等 / 办完了 / 出问题了。
 */
export type StatusTone = "idle" | "active" | "hold" | "done" | "bad";

export const STATUS_TONES: Record<string, StatusTone> = {
  new: "idle",
  understanding: "active",
  planning: "active",
  acting: "active",
  awaiting_approval: "hold",
  escalated: "hold",
  resolved: "done",
  closed: "done",
  failed: "bad",
};

export const statusLabel = (status: TicketStatus | null | undefined) =>
  STATUS_LABELS[status ?? ""] ?? status ?? UNKNOWN;

export const statusTone = (status: TicketStatus | null | undefined): StatusTone =>
  STATUS_TONES[status ?? ""] ?? "idle";

/** 这两档意味着"有人在等这件事"，是整个台面唯一允许持续动的地方 */
export const isWaitingOnHuman = (status: TicketStatus | null | undefined) =>
  status === "awaiting_approval" || status === "escalated";

export const RISK_LABELS: Record<string, string> = {
  low: "低风险",
  mid: "中风险",
  high: "高风险",
};

export const RISK_TONES: Record<string, StatusTone> = {
  low: "active",
  mid: "hold",
  high: "bad",
};

export const riskLabel = (risk: TicketRisk | null | undefined) =>
  risk ? RISK_LABELS[risk] ?? risk : UNKNOWN;

export const riskTone = (risk: TicketRisk | null | undefined): StatusTone =>
  RISK_TONES[risk ?? ""] ?? "idle";

export const CHANNEL_LABELS: Record<string, string> = {
  web_chat: "网页聊天",
  email: "邮件",
  app: "APP",
  wecom: "企业微信",
  phone: "电话转写",
  api: "接口",
};

export const channelLabel = (
  channel: TicketChannel | string | null | undefined
) => CHANNEL_LABELS[channel ?? ""] ?? channel ?? UNKNOWN;

export const INTENT_LABELS: Record<string, string> = {
  query_order: "查订单",
  query_logistics: "查物流",
  change_address: "改地址",
  refund: "退款",
  cancel_order: "取消订单",
  invoice: "开票",
  complaint: "投诉",
  other: "其他",
};

export const intentLabel = (intent: string | null | undefined) =>
  INTENT_LABELS[intent ?? ""] ?? intent ?? UNKNOWN;

export const ESCALATION_LABELS: Record<string, string> = {
  model_unavailable: "模型通道故障",
  model_protocol_error: "模型返回格式异常",
  tool_failures: "工具连续失败",
  tool_budget: "工具调用次数用尽",
  cost_budget: "单工单成本触顶",
  approval_rejected: "人工拒绝后再提",
  governor_blocked: "被治理拦下",
  policy_requires_human: "政策要求人工把关",
  sla_overdue: "SLA 到点",
  not_resolved: "未能自动解决",
};

export const escalationLabel = (reason: string | null | undefined) =>
  ESCALATION_LABELS[reason ?? ""] ?? reason ?? "";

// ========== 权限档位 ==========

export const TIER_LABELS: Record<string, string> = {
  read: "查询",
  mutate: "修改",
  fund: "资金",
};

export const TIER_HINTS: Record<string, string> = {
  read: "只读，不打断人",
  mutate: "会留台账，自动执行",
  fund: "必须有人点头才执行",
};

export const tierLabel = (tier: ToolTier | string | null | undefined) =>
  TIER_LABELS[tier ?? ""] ?? tier ?? UNKNOWN;

/** 未知工具按最重的档处理：宁可高估一次打扰，也不要低估一次风险 */
export const tierTone = (tier: ToolTier | string | null | undefined): StatusTone =>
  tier === "fund" ? "bad" : tier === "mutate" ? "hold" : "active";

// ========== 编排节点与事件类型 ==========

/** 轨迹回放里每一步属于哪个阶段（对齐后端 trace.NODES） */
export const NODE_LABELS: Record<string, string> = {
  intake: "接单",
  understand: "理解",
  risk: "风险判定",
  retrieve: "检索",
  plan: "规划",
  act: "执行",
  confirm: "收尾",
  escalate: "转人工",
};

export const nodeLabel = (node: string | null | undefined) =>
  NODE_LABELS[node ?? ""] ?? node ?? "";

/** 一步的性质（对齐后端 trace.KINDS） */
export const KIND_LABELS: Record<string, string> = {
  thinking: "判断",
  tool_call: "发起调用",
  tool_result: "调用结果",
  approval: "人工处置",
  decision: "结论",
  state_change: "状态变更",
  error: "异常",
  reply: "答复客户",
};

export const kindLabel = (kind: string | null | undefined) =>
  KIND_LABELS[kind ?? ""] ?? kind ?? "";

/**
 * 工具名 → 人话。**审批卡片上这一列最要紧**：坐席要在那里判断"同不同意"，
 * 标题写着 `create_refund` 帮不上忙，写着"发起退款"才帮得上。
 *
 * 未登记的名字原样显示——后端加了新工具时，界面上不该出现空白。
 */
export const TOOL_LABELS: Record<string, string> = {
  lookup_order: "查订单",
  lookup_logistics: "查物流",
  lookup_customer: "查客户档案",
  update_order_address: "修改收货信息",
  request_invoice: "提交发票申请",
  create_refund: "发起退款",
  cancel_order: "取消订单",
  search_policy: "检索政策依据",
  load_skill: "加载作业指导",
  read_skill_file: "读取指导附件",
  delegate: "委派子代理",
};

export const toolLabel = (tool?: string | null) =>
  TOOL_LABELS[tool ?? ""] ?? tool ?? "工具";

/** 子代理角色名（后端 agent_roles.ROLES） */
export const ROLE_LABELS: Record<string, string> = {
  inquiry: "查询",
  policy: "政策",
  reassurance: "安抚",
};

export const roleLabel = (role?: string | null) =>
  ROLE_LABELS[role ?? ""] ?? role ?? "";

/**
 * 埋点 span 名 → 中文。未登记的原样显示，避免加了新埋点就"消失"。
 *
 * 子代理那一层的 span 名是 `agent.<role>` 和 `<role>.round`，两种都从角色名推导，
 * 所以新加一个角色时这里不需要跟着改。
 */
export const SPAN_LABELS: Record<string, string> = {
  "ticket.reason": "主代理决策",
  "ticket.understand": "工单理解",
  "ticket.plan": "任务规划",
  "llm.chat": "模型调用",
  "llm.structured": "结构化输出",
  "retrieval.hybrid": "混合检索",
  "retrieval.dense": "向量检索",
  "embedding.embed": "文本向量化",
};

export const spanLabel = (name: string) => {
  if (SPAN_LABELS[name]) return SPAN_LABELS[name];
  if (name.startsWith("tool.")) return `工具 · ${name.slice(5)}`;
  if (name.startsWith("agent.")) return `子代理 · ${roleLabel(name.slice(6))}`;
  if (name.endsWith(".round")) return `${roleLabel(name.slice(0, -6))} · 轮次`;
  return name;
};

// ========== SLA ==========

export interface SlaView {
  /** 已经超期 */
  overdue: boolean;
  /** 剩余时间的中文说法。没有 slaDueAt 时为 null —— 显示"不设 SLA"而不是"0 分钟" */
  remaining: string | null;
  tone: StatusTone;
}

/**
 * SLA 的读法。
 *
 * 用**本地当前时间**比 `slaDueAt`：后端写的是 naive 墙上时间，同机部署时浏览器
 * 读到的就是同一套钟。不做时区换算——换算本身才是引入错误的地方。
 */
export const slaView = (
  slaDueAt: string | null | undefined,
  status: TicketStatus | null | undefined
): SlaView => {
  const idle = { overdue: false, remaining: null, tone: "idle" as StatusTone };
  if (!slaDueAt) return idle;
  const due = new Date(slaDueAt).getTime();
  if (Number.isNaN(due)) return idle;
  if (status === "resolved" || status === "closed") {
    return { overdue: false, remaining: null, tone: "done" };
  }
  const delta = due - Date.now();
  if (delta <= 0) {
    return {
      overdue: true,
      remaining: `超期 ${humanizeSpan(-delta)}`,
      tone: "bad",
    };
  }
  return {
    overdue: false,
    remaining: `剩 ${humanizeSpan(delta)}`,
    // 两小时以内算"紧"：这一档要能在队列里一眼跳出来
    tone: delta < 2 * 60 * 60 * 1000 ? "hold" : "active",
  };
};

function humanizeSpan(ms: number): string {
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时`;
  return `${Math.round(hours / 24)} 天`;
}
