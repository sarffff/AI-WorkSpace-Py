import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiClient } from "@/shared/api/client";
import type { TraceSummary } from "@/shared/types/api.types";
import { fmtInt, fmtMs } from "@/shared/lib/format";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import {
  Bot,
  BookOpen,
  RefreshCw,
  ShieldAlert,
  Zap,
  X,
} from "lucide-react";

/** SSE 流式事件摘要,由 ChatPage 收集后传入 */
export interface InsightEvent {
  type:
    | "tool_start"
    | "tool_result"
    | "citations"
    | "context_compacted"
    | "guardrail"
    | "cache_hit"
    | "agent_step"
    | "agent_state"
    | "done";
  label: string;
  status?: string;
  detail?: string;
}

interface Props {
  sessionId: string | null;
  events: InsightEvent[];
  onClose?: () => void;
}

export const ChatInsightPanel: React.FC<Props> = ({
  sessionId,
  events,
  onClose,
}) => {
  const navigate = useNavigate();
  const toast = useToast();
  const [sessionTraces, setSessionTraces] = useState<TraceSummary[]>([]);
  const [tracesLoading, setTracesLoading] = useState(false);

  useEffect(() => {
    if (!sessionId) return;
    setTracesLoading(true);
    apiClient
      .getTraces(sessionId, 5)
      .then(setSessionTraces)
      .catch((e) => {
        toast.error(toastMessageFrom(e, "加载运行轨迹失败"));
      })
      .finally(() => setTracesLoading(false));
  }, [sessionId, toast]);

  return (
    <div className="w-72 border-l border-[#e6e2d8] dark:border-[#282724] bg-[#f3f0e6]/40 dark:bg-[#1a1917]/40 backdrop-blur-sm flex flex-col h-full overflow-y-auto">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#e6e2d8] dark:border-[#282724]">
        <div>
          <div className="label-eyebrow">Insight</div>
          <span className="text-xs font-semibold text-[#1f1e1d] dark:text-[#edece8]">
            运行洞察
          </span>
        </div>
        {onClose && (
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-5">
        {/* 本轮过程 */}
        {events.length > 0 && (
          <div className="space-y-2.5">
            <h4 className="text-[10px] font-semibold uppercase tracking-wider text-[#918d83]">
              本轮过程
            </h4>
            <div className="rail-track space-y-1.5 pl-1">
              {events.map((evt, i) => (
                <div
                  key={i}
                  className="relative flex items-center gap-2 text-[11px] px-2.5 py-1.5 rounded-lg bg-[#faf9f5] dark:bg-[#191817] anim-fade-up"
                  style={{ animationDelay: `${i * 0.04}s` }}
                >
                  {evt.type === "tool_start" && (
                    <span className="w-1.5 h-1.5 rounded-full bg-[#da7756] animate-pulse shrink-0" />
                  )}
                  {evt.type === "tool_result" && (
                    <span className="text-emerald-500 shrink-0">✓</span>
                  )}
                  {evt.type === "citations" && (
                    <BookOpen className="w-3 h-3 text-[#da7756] shrink-0" />
                  )}
                  {evt.type === "context_compacted" && (
                    <span className="text-[10px] shrink-0">⌫</span>
                  )}
                  {evt.type === "guardrail" && (
                    <ShieldAlert className="w-3 h-3 text-amber-500 shrink-0" />
                  )}
                  {evt.type === "cache_hit" && (
                    <Zap className="w-3 h-3 text-sky-500 shrink-0" />
                  )}
                  {evt.type === "agent_step" && (
                    <Bot className="w-3 h-3 text-violet-500 shrink-0" />
                  )}
                  {evt.type === "agent_state" && (
                    <span
                      className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                        evt.status === "failed"
                          ? "bg-rose-500"
                          : evt.status === "completed"
                            ? "bg-emerald-500"
                            : "bg-violet-500 animate-pulse"
                      }`}
                    />
                  )}
                  {evt.type === "done" && (
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0" />
                  )}
                  <span className="text-[#1f1e1d] dark:text-[#edece8] truncate">
                    {evt.label}
                  </span>
                  {evt.detail && (
                    <span className="text-[10px] text-[#918d83] shrink-0">
                      {evt.detail}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 本会话 trace */}
        {sessionId && (
          <div className="space-y-2.5">
            <h4 className="text-[10px] font-semibold uppercase tracking-wider text-[#918d83]">
              本会话历史
            </h4>
            {tracesLoading && (
              <div className="text-[11px] text-[#918d83]">加载中...</div>
            )}
            {!tracesLoading && sessionTraces.length === 0 && (
              <div className="text-[11px] text-[#918d83]">暂无记录</div>
            )}
            <div className="space-y-1">
              {sessionTraces.map((t) => (
                <button
                  key={t.traceId}
                  onClick={() => navigate(`/traces?trace=${t.traceId}`)}
                  className="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[11px] text-left transition-colors"
                >
                  <span
                    className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                      t.failures > 0 ? "bg-rose-500" : "bg-emerald-500"
                    }`}
                  />
                  <span className="flex-1 truncate text-[#6e6b63] dark:text-[#a19f96]">
                    {t.startedAt
                      ? new Date(t.startedAt).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })
                      : t.traceId.slice(0, 8)}
                  </span>
                  <span className="text-[#1f1e1d] dark:text-[#edece8]">
                    {fmtMs(t.durationMs)}
                  </span>
                  <span className="text-[#918d83]">
                    {fmtInt(t.promptTokens + t.completionTokens)} tokens
                  </span>
                </button>
              ))}
            </div>
            {sessionTraces.length > 0 && (
              <button
                onClick={() => navigate(`/traces?chat=${sessionId}`)}
                className="text-[11px] text-[#da7756] hover:underline flex items-center gap-1"
              >
                <RefreshCw className="w-3 h-3" />
                查看完整轨迹
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
