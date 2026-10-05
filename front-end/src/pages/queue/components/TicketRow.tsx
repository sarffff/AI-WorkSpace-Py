import React from "react";
import {
  channelLabel,
  escalationLabel,
  intentLabel,
  isWaitingOnHuman,
  riskLabel,
  slaView,
  statusLabel,
  statusTone,
} from "@/shared/lib/format";
import type { TicketSummary } from "@/shared/types/api.types";
import { StatusPill } from "@/shared/ui/StatusPill";

interface TicketRowProps {
  ticket: TicketSummary;
  onOpen: (ticketId: string) => void;
}

/**
 * 队列里的一行。
 *
 * 左沿那条色是**风险档**，不是状态：状态已经有胶囊说了，而风险说的是"这单碰错了
 * 会有多疼"。扫队列时眼睛先碰到色条，这正是希望它发生的顺序。
 *
 * 金额与编号用等宽（`.num`），一是为了对齐，二是因为核对一个字符时比例字体的
 * `1` 和 `l` 会读错。
 */
export const TicketRow: React.FC<TicketRowProps> = ({ ticket, onOpen }) => {
  const tone = statusTone(ticket.status);
  const waiting = isWaitingOnHuman(ticket.status);
  const sla = slaView(ticket.slaDueAt, ticket.status);

  return (
    <button
      type="button"
      onClick={() => onOpen(ticket.id)}
      className="ledger-row w-full text-left gap-4 py-3 px-3 tier-bar queue-row"
      data-tier={ticket.riskLevel === "high" ? "fund" : ticket.riskLevel === "mid" ? "mutate" : "read"}
      aria-label={`打开工单 ${ticket.id}`}
    >
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2">
          <span className="num text-[10.5px] text-ink-faint truncate">
            {ticket.id.slice(0, 8)}
          </span>
          <span className="text-[10.5px] text-ink-faint">{channelLabel(ticket.channel)}</span>
          <span className="text-[10.5px] text-ink-faint">·</span>
          <span className="text-[10.5px] text-ink-soft">{intentLabel(ticket.intent)}</span>
        </span>
        <span className="block text-[13.5px] font-medium text-ink truncate mt-0.5">
          {ticket.subject || ticket.summary || "（无标题）客户原文见详情"}
        </span>
        {ticket.escalationReason && (
          <span className="block text-[11px] text-state-bad truncate mt-0.5">
            {escalationLabel(ticket.escalationReason)}
          </span>
        )}
      </span>

      <span className="hidden md:block w-[88px] shrink-0">
        <span className="block text-[10.5px] text-ink-faint mb-1">
          {riskLabel(ticket.riskLevel)}
        </span>
        <span className="num text-[11px] text-ink-soft">
          {ticket.toolRounds ? `${ticket.toolRounds} 轮` : "—"}
        </span>
      </span>

      <span className="w-[110px] shrink-0 flex justify-end">
        <StatusPill tone={tone} beacon={waiting}>
          {statusLabel(ticket.status)}
        </StatusPill>
      </span>

      <span className="w-[104px] shrink-0 text-right">
        <span
          className="num block text-[11.5px]"
          style={{
            color:
              sla.tone === "bad"
                ? "var(--c-bad)"
                : sla.tone === "hold"
                  ? "var(--c-hold)"
                  : "var(--c-ink-soft)",
          }}
        >
          {sla.remaining ?? "无 SLA"}
        </span>
        <span className="block text-[10.5px] text-ink-faint">
          {ticket.csatScore ? `CSAT ${ticket.csatScore}/5` : "未评价"}
        </span>
      </span>
    </button>
  );
};
