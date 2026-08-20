/* 共享的标签与格式化辅助：Body / RoleBars / Comparison 共用 */

export const AGENT_LABELS: Record<string, string> = {
  researcher: "资料研究员",
  analyst: "分析员",
  critic: "审阅员",
};

export const agentLabel = (role?: string | null) =>
  role ? (AGENT_LABELS[role] ?? role) : "主代理";

export const MODE_LABELS: Record<string, string> = {
  off: "未开启",
  augment: "augment（自主判断）",
  supervisor: "supervisor（工具归角色）",
  write: "write（写操作需审批）",
  listed: "listed（白名单）",
};

/** null 与 0 必须显示成不同的东西：前者是缺数据，后者是真的为零 */
export const fmtRate = (rate: number | null) =>
  rate === null ? "—" : `${(rate * 100).toFixed(1)}%`;
