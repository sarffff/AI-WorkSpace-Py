import React, { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  Gauge,
  Loader2,
  MailWarning,
  PauseOctagon,
  PlayCircle,
  Search,
  Send,
  X,
} from "lucide-react";
import { apiClient, isConflictResponse } from "@/shared/api/client";
import { fmtMoney, fmtRelativeTime, toolLabel } from "@/shared/lib/format";
import { PageHeader } from "@/shared/ui/PageHeader";
import { StatusPill } from "@/shared/ui/StatusPill";
import { TierMark } from "@/shared/ui/TierMark";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import type {
  GovernorState,
  OperationRow,
  OutboxItem,
} from "@/shared/types/api.types";

/**
 * 治理台：文档§6 那三条（权限、预算与熔断、完整审计）里能被"操作"的两条。
 *
 * 三段是一件事的三段：**限额**（今天还能退多少）、**暂停**（现在一律不许动）、
 * **复核**（已经动过的那些有没有动错）。分开放在三个页面里，出事时会多花
 * 两次跳转去找，而那两次跳转正是故障处置最贵的时间。
 */
export const GovernancePage: React.FC = () => {
  const toast = useToast();

  const [governor, setGovernor] = useState<GovernorState | null>(null);
  const [outbox, setOutbox] = useState<{ items: OutboxItem[]; pending: number; senderConnected: boolean }>({
    items: [],
    pending: 0,
    senderConnected: false,
  });
  const [operations, setOperations] = useState<OperationRow[]>([]);
  const [pausedNote, setPausedNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  /** 首屏还没回来时不给任何"空"的断言：没有数据 ≠ 没有欠客户的话 */
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const [state, queue, unreviewed] = await Promise.all([
        apiClient.governorState(),
        apiClient.outboxQueue(),
        apiClient.unreviewedOperations(7, 50),
      ]);
      if (isConflictResponse(state) || isConflictResponse(queue) || isConflictResponse(unreviewed)) {
        setUnavailable(
          (isConflictResponse(state) ? state.message : "工单能力未开启") ??
            "工单能力未开启"
        );
        return;
      }
      setUnavailable(null);
      setGovernor(state);
      setOutbox(queue);
      setOperations(unreviewed.items);
      setLoaded(true);
    } catch (e) {
      toast.error(toastMessageFrom(e, "治理台读取失败"));
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const pause = async (on: boolean) => {
    if (!pausedNote.trim()) {
      toast.error("必须写理由。暂停是个决定，审计里要知道是谁因为什么按下的。");
      return;
    }
    setBusy(on ? "pause" : "resume");
    try {
      const result = on
        ? await apiClient.pauseGovernor(pausedNote.trim())
        : await apiClient.resumeGovernor(pausedNote.trim());
      setGovernor((previous) =>
        previous ? { ...previous, paused: result.paused, pauseReason: result.pauseReason } : previous
      );
      setPausedNote("");
      toast.success(result.paused ? "已全线暂停" : "已恢复");
    } catch (e) {
      toast.error(toastMessageFrom(e, "操作失败"));
    } finally {
      setBusy(null);
    }
  };

  const drain = async () => {
    setBusy("drain");
    try {
      const result = await apiClient.drainOutbox(20);
      const queue = await apiClient.outboxQueue();
      if (!isConflictResponse(queue)) setOutbox(queue);
      toast.info(
        `本次投递：成功 ${result.sent ?? 0} · 失败 ${result.failed ?? 0}${
          result.sent === 0 && result.failed === 0 ? "（没有可用发送通道）" : ""
        }`
      );
    } catch (e) {
      toast.error(toastMessageFrom(e, "投递失败"));
    } finally {
      setBusy(null);
    }
  };

  const suppress = async (row: OutboxItem) => {
    const reason = window.prompt("为什么不发这一条？理由会进审计。");
    if (!reason || !reason.trim()) return;
    setBusy(`suppress:${row.id}`);
    try {
      const queue = await apiClient.suppressOutbox(row.id, reason.trim());
      if (!isConflictResponse(queue)) setOutbox(queue);
      toast.success("已抑制");
    } catch (e) {
      toast.error(toastMessageFrom(e, "抑制失败"));
    } finally {
      setBusy(null);
    }
  };

  const review = async (row: OperationRow, verdict: "ok" | "wrong") => {
    let action = "";
    if (verdict === "wrong") {
      action = window.prompt("实际怎么补救的？（≤40 字，这一列是错误操作率的分类依据）") ?? "";
      if (!action.trim()) return;
    }
    setBusy(`review:${row.id}`);
    try {
      await apiClient.reviewOperation(row.id, { verdict, action: action.trim() });
      setOperations((previous) => previous.filter((item) => item.id !== row.id));
      toast.success(verdict === "ok" ? "已记为处置正确" : "已记为错误处置");
    } catch (e) {
      toast.error(toastMessageFrom(e, "复核记录失败"));
    } finally {
      setBusy(null);
    }
  };

  const used = Number(governor?.refundUsedToday ?? "0");
  const limit = Number(governor?.dailyRefundLimit ?? "0");
  const ratio = limit > 0 ? Math.min(1, used / limit) : 0;

  return (
    <div className="page-shell">
      <div className="max-w-[1080px] mx-auto flex flex-col gap-5">
        <PageHeader
          description="限额、一键暂停、待发队列，以及「已经动过的那些操作有没有动错」的复核。"
          actions={
            <button type="button" className="btn-quiet" onClick={() => void load()}>
              刷新
            </button>
          }
        />

        {unavailable && (
          <div className="card-surface rounded-2xl p-5 text-[13px] text-ink-soft">
            {unavailable}
          </div>
        )}

        {!unavailable && !loaded && (
          <div className="flex items-center justify-center gap-2 py-16 text-ink-soft text-sm">
            <Loader2 className="w-4 h-4 animate-spin" />
            正在读取治理状态…
          </div>
        )}

        {!unavailable && loaded && (
          <>
            <section className="card-surface rounded-2xl p-5 flex flex-col gap-4">
              <div className="flex items-center gap-2">
                <Gauge className="w-4 h-4 text-accent" />
                <h2 className="text-[14px] font-semibold text-ink">当日退款额度</h2>
                <span className="num text-[12px] text-ink-faint ml-auto">
                  {fmtMoney(governor?.refundUsedToday ?? null)} / {fmtMoney(governor?.dailyRefundLimit ?? null)}
                </span>
              </div>

              <div
                className="gauge"
                data-critical={ratio >= 0.8 ? "true" : "false"}
                data-empty={used === 0 ? "true" : "false"}
                role="progressbar"
                aria-valuenow={Math.round(ratio * 100)}
                aria-valuemin={0}
                aria-valuemax={100}
              >
                <i style={{ width: `${ratio * 100}%` }} />
              </div>

              <div className="grid sm:grid-cols-3 gap-3 text-[12px]">
                <Cell label="单工单成本上限" value={governor?.maxCostPerTicket ? fmtMoney(governor.maxCostPerTicket) : "未设（不熔断）"} />
                <Cell label="单工单工具调用" value={governor?.perTicketToolCalls ? `${governor.perTicketToolCalls} 次` : "未设"} />
                <Cell label="退款需人审阈值" value={fmtMoney(governor?.refundReviewThreshold ?? null)} />
              </div>

              <p className="text-[11px] text-ink-faint leading-relaxed">
                额度用满后 Agent 不再执行任何退款，而不是"退少一点"。这两个行为差很多：
                后者会让客户收到一笔和答复不符的钱。
              </p>

              <div className="flex items-center gap-2 flex-wrap pt-1 border-t border-line">
                {governor?.paused ? (
                  <PlayCircle className="w-4 h-4 text-state-done" />
                ) : (
                  <PauseOctagon className="w-4 h-4 text-state-bad" />
                )}
                <input
                  className="input-field flex-1 min-w-[220px]"
                  placeholder={governor?.paused ? "恢复的理由" : "暂停的理由（必填）"}
                  value={pausedNote}
                  maxLength={200}
                  onChange={(event) => setPausedNote(event.target.value)}
                />
                {governor?.paused ? (
                  <button
                    type="button"
                    className="btn-accent px-3.5 py-2 rounded-[10px] text-[12px] font-semibold"
                    disabled={busy !== null}
                    onClick={() => void pause(false)}
                  >
                    {busy === "resume" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                    恢复运行
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn-alarm"
                    disabled={busy !== null}
                    onClick={() => void pause(true)}
                  >
                    {busy === "pause" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                    全线暂停
                  </button>
                )}
              </div>
            </section>

            <section className="card-surface rounded-2xl overflow-hidden">
              <div className="flex items-center gap-2 px-5 py-3.5 border-b border-line">
                <Send className="w-4 h-4 text-accent" />
                <h2 className="text-[14px] font-semibold text-ink">待发回复</h2>
                <span className="num text-[11.5px] text-ink-faint">待发送 {outbox.pending}</span>
                <button
                  type="button"
                  className="btn-quiet ml-auto py-1"
                  disabled={busy !== null}
                  onClick={() => void drain()}
                >
                  {busy === "drain" ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
                  催一次投递
                </button>
              </div>

              {!outbox.senderConnected && (
                <div
                  className="flex items-start gap-2 px-5 py-3 text-[12px]"
                  style={{ background: "var(--c-hold-faint)", color: "var(--c-hold)" }}
                >
                  <MailWarning className="w-4 h-4 shrink-0 mt-0.5" />
                  <span>
                    没有接入任何发送通道（邮件 / 企微 / 短信）。这些话已经落在待发队列里，
                    但**不会发出去**——催投递不改任何状态。这不是发送失败，是没有出口。
                  </span>
                </div>
              )}

              {outbox.items.length === 0 ? (
                <div className="px-5 py-8 text-center text-[12px] text-ink-faint">
                  没有欠客户的话。
                </div>
              ) : (
                outbox.items.map((item) => (
                  <div key={item.id} className="px-5 py-3 border-b border-line last:border-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="num text-[10.5px] text-ink-faint">
                        {item.ticketId.slice(0, 8)}
                      </span>
                      <span className="text-[12px] text-ink">
                        {item.kind === "csat_invite" ? "满意度邀请" : "答复客户"}
                      </span>
                      <span className="text-[11px] text-ink-faint">→ {item.recipient ?? "无收件人"}</span>
                      <StatusPill
                        tone={
                          item.status === "sent"
                            ? "done"
                            : item.status === "failed"
                              ? "bad"
                              : item.status === "suppressed"
                                ? "idle"
                                : "hold"
                        }
                      >
                        {item.status}
                      </StatusPill>
                      {item.attempts > 0 && (
                        <span className="num text-[10.5px] text-ink-faint">
                          试过 {item.attempts} 次
                        </span>
                      )}
                      {item.status !== "sent" && item.status !== "suppressed" && (
                        <button
                          type="button"
                          className="btn-alarm ml-auto py-1"
                          disabled={busy !== null}
                          onClick={() => void suppress(item)}
                        >
                          <X className="w-3 h-3" />
                          不发这条
                        </button>
                      )}
                    </div>
                    {item.error && (
                      <p className="text-[11px] text-state-bad mt-1">{item.error}</p>
                    )}
                    {item.body && (
                      <p className="text-[11.5px] text-ink-soft mt-1.5 line-clamp-2 break-words">
                        {item.body}
                      </p>
                    )}
                  </div>
                ))
              )}
            </section>

            <section className="card-surface rounded-2xl overflow-hidden">
              <div className="flex items-center gap-2 px-5 py-3.5 border-b border-line">
                <Search className="w-4 h-4 text-accent" />
                <h2 className="text-[14px] font-semibold text-ink">待复核的操作</h2>
                <span className="num text-[11.5px] text-ink-faint">{operations.length} 笔</span>
                <span className="ml-auto text-[11px] text-ink-faint">
                  错误操作率的分母来自这里，没人复核时它是 null 而不是 0%
                </span>
              </div>

              {operations.length === 0 ? (
                <div className="px-5 py-8 text-center text-[12px] text-ink-faint">
                  最近 7 天没有待复核的执行记录。
                </div>
              ) : (
                operations.map((row) => (
                  <div key={row.id} className="px-5 py-3 border-b border-line last:border-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <TierMark tier={row.permission} />
                      <span className="text-[12.5px] font-medium text-ink">
                        {toolLabel(row.tool ?? undefined)}
                      </span>
                      {row.amount && (
                        <span className="num text-[12px] text-state-bad">{fmtMoney(row.amount, row.currency)}</span>
                      )}
                      <span className="text-[10.5px] text-ink-faint">
                        {fmtRelativeTime(row.createdAt)}
                      </span>
                      <span className="ml-auto flex items-center gap-1.5">
                        <button
                          type="button"
                          className="btn-quiet py-1"
                          disabled={busy !== null}
                          onClick={() => void review(row, "ok")}
                        >
                          <Check className="w-3 h-3" />
                          处置正确
                        </button>
                        <button
                          type="button"
                          className="btn-alarm py-1"
                          disabled={busy !== null}
                          onClick={() => void review(row, "wrong")}
                        >
                          <AlertTriangle className="w-3 h-3" />
                          判错了
                        </button>
                      </span>
                    </div>
                    {(row.argumentsPreview || row.resultPreview) && (
                      <p className="num text-[11px] text-ink-faint mt-1.5 line-clamp-2 break-all">
                        {row.argumentsPreview}
                        {row.resultPreview ? ` ⟶ ${row.resultPreview}` : ""}
                      </p>
                    )}
                  </div>
                ))
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
};

const Cell: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="rounded-xl border border-line px-3 py-2">
    <div className="text-[10.5px] text-ink-faint">{label}</div>
    <div className="num text-[13px] text-ink mt-0.5">{value}</div>
  </div>
);
