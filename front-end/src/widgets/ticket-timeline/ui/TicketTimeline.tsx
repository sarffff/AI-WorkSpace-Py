import React, { useMemo, useState } from "react";
import { ChevronDown } from "lucide-react";
import { kindLabel, nodeLabel, roleLabel, toolLabel } from "@/shared/lib/format";
import type { TicketTraceEvent } from "@/shared/types/api.types";

interface TimelineProps {
  events: TicketTraceEvent[];
}

/** 事件状态到语气档。后端的 status 取值是固定集合，未登记按"没人管"处理 */
const TONE_BY_STATUS: Record<string, string> = {
  ok: "done",
  error: "bad",
  blocked: "bad",
  rejected: "hold",
  pending: "idle",
};

const VAR_BY_TONE: Record<string, string> = {
  done: "var(--c-done)",
  bad: "var(--c-bad)",
  hold: "var(--c-hold)",
  idle: "var(--c-idle)",
  active: "var(--c-active)",
};

/**
 * 这一步是哪个子代理做的，只能从说明文字里读。
 *
 * 事件的 `tool` 列是工具名（`lookup_order`），不带角色；轨迹里子代理那几步的
 * message 固定以"子代理 <role> 的第 N 轮"开头（见编排层的落库点）。没有专门的列
 * 是后端的选择——那一步的归属已经在轨迹的顺序里了。所以这里按前缀取，取不到就不标。
 */
function roleInMessage(message: string | null): string | null {
  if (!message || !message.startsWith("子代理 ")) return null;
  const [, role] = message.split(" ");
  return role ?? null;
}

/**
 * 工单轨迹。文档§2 的"观测"要求每一步思考、调用、结果都能回放，
 * 所以这里的读法是按**工序**排的，不是按时间戳：
 *
 * 左侧序号槽 = 这一步在第几步；节点徽标 = 哪个阶段；`act` 那一档再细分工具与轮次。
 * 时间戳只在悬浮里——同一步里连写两条时 naive DATETIME 没有亚秒精度，
 * 看时间排序会把两件事读成一件事（后端 trace.replay 的注释记着同一件事）。
 */
export const TicketTimeline: React.FC<TimelineProps> = ({ events }) => {
  const [onlyActions, setOnlyActions] = useState(false);

  const shown = useMemo(
    () =>
      onlyActions
        ? events.filter((event) =>
            ["tool_call", "tool_result", "approval", "decision", "reply", "error"].includes(
              event.kind
            )
          )
        : events,
    [events, onlyActions]
  );

  if (events.length === 0) {
    return (
      <div className="card-surface rounded-2xl p-6 text-center">
        <p className="text-[13px] text-ink-soft">还没有轨迹。</p>
        <p className="text-[11.5px] text-ink-faint mt-1">
          工单一旦被驱动，每一步判断与调用都会落到这里。
        </p>
      </div>
    );
  }

  return (
    <div className="card-surface rounded-2xl overflow-hidden">
      <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-b border-line">
        <div className="label-eyebrow">PROCESS TRACE · {events.length} 步</div>
        <button
          type="button"
          onClick={() => setOnlyActions((value) => !value)}
          className="text-[11px] px-2 py-1 rounded-lg border border-line text-ink-soft hover:border-line-strong"
          aria-pressed={onlyActions}
        >
          {onlyActions ? "显示全部步骤" : "只看待处置与结果"}
        </button>
      </div>

      <div className="rail-track pl-6 pr-4 py-3 flex flex-col">
        {shown.map((event) => {
          const tone = TONE_BY_STATUS[event.status ?? ""] ?? "idle";
          const role = roleInMessage(event.message);
          const detail = event.result || event.argumentsPreview || "";
          return (
            <div key={event.seq} className="relative py-2 border-b border-line last:border-b-0">
              <div className="flex items-start gap-2.5">
                <span className="trace-gutter w-8 shrink-0 pt-0.5">{event.seq}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span className="trace-node">{nodeLabel(event.node)}</span>
                    <span className="text-[11px] text-ink-soft">{kindLabel(event.kind)}</span>
                    {event.tool && (
                      <span className="text-[11px] font-medium text-ink">
                        · {toolLabel(event.tool)}
                      </span>
                    )}
                    {role && <span className="chip text-[9px]">子代理 {roleLabel(role)}</span>}
                    {event.roundIndex ? (
                      <span className="text-[10.5px] text-ink-faint num">
                        第 {event.roundIndex} 轮
                      </span>
                    ) : null}
                    <span
                      className="ml-auto text-[10px] font-semibold"
                      style={{ color: VAR_BY_TONE[tone] ?? VAR_BY_TONE.idle }}
                    >
                      {event.status ?? ""}
                    </span>
                  </div>

                  {event.message && (
                    <p className="text-[12.5px] text-ink mt-1 leading-relaxed whitespace-pre-wrap">
                      {event.message}
                    </p>
                  )}

                  {detail && (
                    <details className="mt-1.5 group">
                      <summary className="cursor-pointer list-none inline-flex items-center gap-1 text-[11px] text-ink-faint hover:text-ink-soft">
                        <ChevronDown className="w-3 h-3 transition-transform group-open:rotate-180" />
                        参数与返回
                        {event.argumentsDigest && (
                          <span className="num ml-1 text-[10px]">
                            #{event.argumentsDigest.slice(0, 8)}
                          </span>
                        )}
                      </summary>
                      <pre className="num mt-2 text-[11px] leading-relaxed whitespace-pre-wrap break-all rounded-lg p-2.5 max-h-64 overflow-auto"
                        style={{ background: "var(--hl-code-bg)", color: "var(--hl-code-ink)" }}
                      >
                        {event.argumentsPreview ? `${event.argumentsPreview}\n\n` : ""}
                        {event.result ?? ""}
                      </pre>
                    </details>
                  )}

                  <div className="text-[10px] text-ink-faint mt-1">
                    {event.createdAt ? new Date(event.createdAt).toLocaleString("zh-CN", { hour12: false }) : ""}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
