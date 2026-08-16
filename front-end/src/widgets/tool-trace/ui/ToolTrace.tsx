import React from "react";
import {
  AlertTriangle,
  Bot,
  ChevronDown,
  ChevronRight,
  Loader2,
  Wrench,
  XCircle,
} from "lucide-react";
import type { ToolStep } from "@/shared/types/api.types";

/**
 * 一个回合的工具轨迹。
 *
 * 存在的理由是刷新页面：SSE 里的 tool_start / tool_result 是瞬时事件，页面一刷
 * 那条时间线就没了，而「这个答案到底查过什么」是事后唯一能核对的东西。数据来自
 * 后端 message_tool_steps 表（GET /chats/:id/tool-steps），流式期间则由事件实时拼。
 *
 * 默认折叠。工具轨迹是排查用的信息，不是对话内容——每条回答下面顶着一大块
 * 执行日志，真正要读的答案就被挤走了。
 *
 * round 0 单独标成「预检索」：它是配置（RAG_PREFETCH）决定的，不是模型自己决定
 * 要查的。混在一起看会把"系统替它查了一次"误读成"它很会用工具"。
 */

const STATUS_STYLE: Record<
  string,
  { label: string; className: string; icon?: React.ReactNode }
> = {
  ok: {
    label: "成功",
    className: "text-emerald-700 dark:text-emerald-400 bg-emerald-500/10",
  },
  invalid_arguments: {
    label: "参数错误",
    className: "text-amber-700 dark:text-amber-400 bg-amber-500/10",
    icon: <AlertTriangle className="w-3 h-3" />,
  },
  unavailable: {
    label: "工具不可用",
    className: "text-rose-700 dark:text-rose-400 bg-rose-500/10",
    icon: <XCircle className="w-3 h-3" />,
  },
  error: {
    label: "执行失败",
    className: "text-rose-700 dark:text-rose-400 bg-rose-500/10",
    icon: <XCircle className="w-3 h-3" />,
  },
};

const RUNNING = {
  label: "执行中",
  className: "text-[#6e6b63] dark:text-[#a19f96] bg-[#e3dfd5]/60 dark:bg-[#2e2d2a]",
  icon: <Loader2 className="w-3 h-3 animate-spin" />,
};

const AGENT_LABELS: Record<string, string> = {
  researcher: "资料研究员",
  analyst: "分析员",
  critic: "审阅员",
};

const agentLabel = (role?: string | null): string =>
  role ? AGENT_LABELS[role] ?? role : "子代理";

/** 把参数对象压成 `key=value` 一行。太长的值截断——完整参数在轨迹接口里。 */
const formatInput = (input?: Record<string, unknown>): string => {
  if (!input) return "";
  const parts = Object.entries(input).map(([key, value]) => {
    const text =
      typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
    return `${key}=${text.length > 60 ? `${text.slice(0, 60)}…` : text}`;
  });
  return parts.join(", ");
};

interface ToolTraceProps {
  steps: ToolStep[];
  /** 后端关掉了 TOOL_HISTORY_ENABLED 时给一句解释，否则用户只看到"没有轨迹" */
  historyDisabled?: boolean;
}

export const ToolTrace: React.FC<ToolTraceProps> = ({
  steps,
  historyDisabled,
}) => {
  const [open, setOpen] = React.useState(false);
  if (!steps.length) return null;

  const modelCalls = steps.filter((step) => step.round > 0);
  const prefetches = steps.length - modelCalls.length;
  const delegated = steps.filter((step) => step.agentRole);
  // 摘要行按出现顺序去重：连着三轮查同一个工具，标题里写三遍没有信息量
  const uniqueTools = Array.from(
    new Set(modelCalls.filter((step) => step.tool !== "delegate").map((step) => step.tool)),
  );
  const agents = Array.from(
    new Set(delegated.map((step) => agentLabel(step.agentRole))),
  );
  const failed = steps.filter(
    (step) => step.status && step.status !== "ok",
  ).length;

  return (
    <div className="rounded-xl border border-[#e3dfd5] dark:border-[#2e2d2a] bg-[#faf9f5] dark:bg-[#191817] overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="w-full flex items-center gap-1.5 px-3 py-2 text-[11px] font-semibold text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#f3f0e6]/50 dark:hover:bg-[#22211e]"
      >
        {open ? (
          <ChevronDown className="w-3 h-3 shrink-0" />
        ) : (
          <ChevronRight className="w-3 h-3 shrink-0" />
        )}
        <Wrench className="w-3 h-3 shrink-0" />
        <span className="shrink-0">工具轨迹 ({steps.length} 步)</span>
        {!open && (
          <span className="min-w-0 truncate font-normal opacity-70">
            {uniqueTools.join(" → ") || "仅系统预检索"}
            {prefetches > 0 ? ` · 预检索 ${prefetches}` : ""}
            {agents.length > 0 ? ` · ${agents.join("、")}` : ""}
          </span>
        )}
        {failed > 0 && (
          <span className="ml-auto shrink-0 font-normal text-rose-600 dark:text-rose-400">
            {failed} 步未成功
          </span>
        )}
      </button>

      {open && (
        <ol className="px-3 pb-2.5 space-y-2">
          {steps.map((step, index) => {
            const style = step.status
              ? STATUS_STYLE[step.status] ?? RUNNING
              : RUNNING;
            const args = formatInput(step.input);
            return (
              <li
                key={step.id ?? `${step.round}-${step.callIndex}-${index}`}
                className={`text-[11px] text-[#6e6b63] dark:text-[#a19f96] ${
                  step.agentRole
                    ? "ml-3 pl-2.5 border-l border-violet-400/40 dark:border-violet-400/30"
                    : ""
                }`}
              >
                <div className="flex items-center gap-1.5 flex-wrap">
                  {step.agentRole ? (
                    <span className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-violet-500/10 text-violet-700 dark:text-violet-300 text-[10px]">
                      <Bot className="w-3 h-3" />
                      {agentLabel(step.agentRole)}
                      {step.agentRound ? ` · 第 ${step.agentRound} 轮` : ""}
                    </span>
                  ) : (
                    <span className="shrink-0 px-1.5 py-0.5 rounded bg-[#e3dfd5] dark:bg-[#2e2d2a] text-[10px]">
                      {step.round === 0 ? "预检索" : `第 ${step.round} 轮`}
                    </span>
                  )}
                  <span className="font-mono text-[#1f1e1d] dark:text-[#edece8]">
                    {step.tool}
                  </span>
                  {step.tool === "delegate" &&
                    typeof step.input?.role === "string" && (
                      <span className="shrink-0 px-1.5 py-0.5 rounded bg-violet-500/10 text-violet-700 dark:text-violet-300 text-[10px]">
                        → {agentLabel(step.input.role)}
                      </span>
                    )}
                  <span
                    className={`shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] ${style.className}`}
                  >
                    {style.icon}
                    {style.label}
                  </span>
                  {!!step.citations?.length && (
                    <span className="shrink-0 opacity-70">
                      命中 {step.citations.length} 处引用
                    </span>
                  )}
                </div>
                {!!args && (
                  <div className="mt-0.5 font-mono break-all opacity-80">
                    {args}
                  </div>
                )}
                {!!step.resultPreview && (
                  <div className="mt-0.5 pl-2 border-l-2 border-[#e3dfd5] dark:border-[#2e2d2a] whitespace-pre-wrap break-words">
                    {step.resultPreview}
                    {typeof step.resultChars === "number" &&
                      step.resultChars > step.resultPreview.length && (
                        <span className="opacity-60">
                          {" "}
                          （原文 {step.resultChars} 字，此处为摘要）
                        </span>
                      )}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}

      {open && historyDisabled && (
        <div className="px-3 pb-2.5 text-[11px] text-amber-700 dark:text-amber-400">
          后端 TOOL_HISTORY_ENABLED 已关闭：这条轨迹只在本次会话内存在，刷新后取不回来，
          模型下一回合也看不到自己这一轮做过什么。
        </div>
      )}
    </div>
  );
};

