import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckSquare2, ShieldCheck } from "lucide-react";
import { apiClient, isConflictResponse } from "@/shared/api/client";
import { fmtMoney, fmtRelativeTime, intentLabel, riskLabel, toolLabel } from "@/shared/lib/format";
import { EmptyState } from "@/shared/ui/EmptyState";
import { PageHeader } from "@/shared/ui/PageHeader";
import { StatusPill } from "@/shared/ui/StatusPill";
import { toastMessageFrom } from "@/shared/ui/Toast";
import { DecisionCard } from "@/widgets/ticket-decision/ui/DecisionCard";
import type { PendingTicket, TicketSummary, ToolTier } from "@/shared/types/api.types";

type Row = TicketSummary & PendingTicket;

/**
 * 审批收件箱。
 *
 * 与队列分开一页，是因为这两种读法不一样：队列按 SLA 与风险排，这里只回答
 * "谁在等我、等什么"。挂起的工单可能等一整天，所以这一页是拉取式的，
 * 不靠那条早已断掉的实时连接。
 *
 * `pending` 为 null 的那些单**列出来但点不开**——工单确实还标着 awaiting_approval，
 * 可从检查点里读不出待批内容（比如后端换过版本）。藏掉它们更糟：那张单会在
 * 界面上彻底消失，而它其实还挂着。
 */
export const ApprovalsPage: React.FC = () => {
  const navigate = useNavigate();
  const [rows, setRows] = useState<Row[]>([]);
  const [tiers, setTiers] = useState<Record<string, ToolTier>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [disabled, setDisabled] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [approvals, queue] = await Promise.all([
        apiClient.getPendingApprovals(),
        apiClient.listTickets({ limit: 1 }).catch(() => null),
      ]);
      if (isConflictResponse(approvals)) {
        setDisabled(approvals.message ?? "工单能力未开启");
        setRows([]);
        return;
      }
      setDisabled(null);
      setRows(approvals.items);
      setSelected((current) =>
        current && approvals.items.some((item) => item.id === current)
          ? current
          : (approvals.items.find((item) => item.pending)?.id ?? null)
      );
      if (queue && !isConflictResponse(queue)) setTiers(queue.toolTiers);
    } catch (e) {
      setError(toastMessageFrom(e, "待批列表读取失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const current = useMemo(
    () => rows.find((item) => item.id === selected) ?? null,
    [rows, selected]
  );

  return (
    <div className="page-shell">
      <div className="max-w-[1180px] mx-auto flex flex-col gap-4">
        <PageHeader
          description="Agent 提出的资金类操作停在这里。批准后才会执行，拒绝后这张单转人工——它不会换个说法再提一遍。"
          actions={
            <button type="button" onClick={() => void load()} className="btn-quiet">
              刷新
            </button>
          }
        />

        {disabled && (
          <EmptyState
            icon={<ShieldCheck className="w-7 h-7 text-accent" />}
            title="工单能力还没开"
            description={disabled}
          />
        )}

        {error && !disabled && (
          <div
            className="px-3 py-2 rounded-xl text-[12px] text-state-bad"
            style={{ background: "var(--c-bad-faint)" }}
          >
            {error}
          </div>
        )}

        {!disabled && !error && rows.length === 0 && !loading && (
          <div className="card-surface rounded-2xl py-14">
            <EmptyState
              icon={<CheckSquare2 className="w-7 h-7 text-state-done" />}
              title="没有等你批的单"
              description="低风险工单 Agent 自己办完；只有资金类，或者它判断该交人的那几步，才会出现在这里。"
            />
          </div>
        )}

        {rows.length > 0 && (
          <div className="grid lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)] gap-4 items-start">
            <div className="card-surface rounded-2xl overflow-hidden">
              {rows.map((row) => {
                const first = row.pending?.calls[0];
                const amount = readAmount(first?.arguments);
                const on = row.id === selected;
                return (
                  <button
                    key={row.id}
                    type="button"
                    onClick={() => setSelected(row.id)}
                    className="ledger-row w-full text-left px-3 py-3 gap-3"
                    data-selected={on ? "true" : "false"}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="num text-[10.5px] text-ink-faint">
                        {row.id.slice(0, 8)}
                      </span>
                      <span className="block text-[13px] font-medium text-ink truncate">
                        {first ? toolLabel(first.name) : "待批内容读不出来"}
                      </span>
                      <span className="block text-[11px] text-ink-soft truncate mt-0.5">
                        {row.subject || row.summary || intentLabel(row.intent)}
                      </span>
                      <span className="block text-[10.5px] text-ink-faint mt-1">
                        {riskLabel(row.riskLevel)} · 挂了 {fmtRelativeTime(row.updatedAt)}
                      </span>
                    </span>
                    <span className="shrink-0 text-right">
                      {amount !== null && (
                        <span className="num block text-[13px] font-semibold text-state-bad">
                          {fmtMoney(amount)}
                        </span>
                      )}
                      <StatusPill tone="hold" beacon>
                        等你批
                      </StatusPill>
                    </span>
                  </button>
                );
              })}
            </div>

            {current?.pending ? (
              <DecisionCard
                key={current.id}
                ticketId={current.id}
                pending={current.pending}
                tiers={tiers}
                onResolved={() => void load()}
              />
            ) : (
              <div className="card-surface rounded-2xl p-6 flex flex-col items-start gap-3">
                <div className="label-eyebrow">无法处置</div>
                <p className="text-[13px] text-ink">
                  这张工单标着「等你批」，但从检查点里读不出待批的内容。
                </p>
                <p className="text-[11.5px] text-ink-soft leading-relaxed">
                  通常是后端版本变更之后遗留的挂起线程。不要猜它要做什么——
                  打开这张单的轨迹，看清楚它停在哪一步，再决定是重跑还是直接人工收尾。
                </p>
                {current && (
                  <button
                    type="button"
                    className="btn-quiet"
                    onClick={() => navigate(`/tickets/${current.id}`)}
                  >
                    看这张单的轨迹
                  </button>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

/** 参数里有没有金额。退款与取消都带，改地址没有——那一行就不显示数字 */
function readAmount(raw?: string): string | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const amount = parsed.amount;
    return typeof amount === "string" || typeof amount === "number"
      ? String(amount)
      : null;
  } catch {
    return null;
  }
}
