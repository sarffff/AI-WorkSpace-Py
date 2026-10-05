import React, { useCallback, useEffect, useState } from "react";
import { Activity, AlertTriangle, BarChart3, Loader2 } from "lucide-react";
import { apiClient, isConflictResponse } from "@/shared/api/client";
import { UNKNOWN, fmtCost, fmtInt, fmtMs, fmtRate } from "@/shared/lib/format";
import { EmptyState } from "@/shared/ui/EmptyState";
import { PageHeader } from "@/shared/ui/PageHeader";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import type { HealthResponse, TicketMetrics, UsageSummary } from "@/shared/types/api.types";

const WINDOWS = [
  { days: 7, label: "7 天" },
  { days: 30, label: "30 天" },
  { days: 90, label: "90 天" },
];

/**
 * 指标看板：文档§1 那五个企业最关心的数。
 *
 * 一条规矩贯穿整页：**null 显示"未知"，不显示 0**。样本不足、价目表没命中、
 * 没人做过复核，这三种情况后端都给 null，而 0 会被读成"完美"。错误操作率那一格
 * 在没有复核时必须是"未知"——它是这条线最不能自欺的指标。
 */
export const MetricsPage: React.FC = () => {
  const toast = useToast();
  const [days, setDays] = useState(30);
  const [metrics, setMetrics] = useState<TicketMetrics | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [ticketMetrics, usageSummary, healthReport] = await Promise.all([
        apiClient.ticketMetrics(days),
        apiClient.getUsage(days),
        // 健康快照是管理员专属：非管理员拿 403 是正常，不是故障，不弹提示
        apiClient.getHealth().catch(() => null),
      ]);
      if (isConflictResponse(ticketMetrics)) {
        setUnavailable(ticketMetrics.message ?? "工单能力未开启");
        setMetrics(null);
        return;
      }
      setUnavailable(null);
      setMetrics(ticketMetrics);
      setUsage(usageSummary);
      setHealth(healthReport && !isConflictResponse(healthReport) ? healthReport : null);
    } catch (e) {
      toast.error(toastMessageFrom(e, "指标读取失败"));
    } finally {
      setLoading(false);
    }
  }, [days, toast]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="page-shell">
      <div className="max-w-[1080px] mx-auto flex flex-col gap-5">
        <PageHeader
          description="自动解决率、人工介入、处理时长、CSAT、错误操作率。窗口外的数字不进这块面板。"
          actions={
            <div className="seg-switch" role="group" aria-label="统计窗口">
              {WINDOWS.map((item) => (
                <button
                  key={item.days}
                  type="button"
                  data-active={days === item.days ? "true" : "false"}
                  onClick={() => setDays(item.days)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          }
        />

        {unavailable && (
          <div className="card-surface rounded-2xl p-5">
            <EmptyState
              icon={<BarChart3 className="w-7 h-7 text-accent" />}
              title="还没有工单指标"
              description={unavailable}
            />
          </div>
        )}

        {!unavailable && loading && (
          <div className="flex items-center justify-center gap-2 py-16 text-ink-soft text-sm">
            <Loader2 className="w-4 h-4 animate-spin" />
            正在汇总…
          </div>
        )}

        {!unavailable && !loading && metrics && (
          <>
            <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3">
              <Big label="自动解决率" value={fmtRate(metrics.deflectionRate)} tone="done"
                hint={`终态 ${metrics.terminal} 张中自己办完的占比`} />
              <Big label="人工介入率" value={fmtRate(metrics.humanHandoffRate)} tone="hold"
                hint="挂审批或转人工，都算打扰到人" />
              <Big label="错误操作率" value={fmtRate(metrics.errorActionRate)} tone="bad"
                hint={
                  metrics.reviewedOperations === 0
                    ? "还没有人做过复核，所以这个数无从谈起"
                    : `已复核 ${metrics.reviewedOperations} 笔`
                } />
              <Big label="平均 CSAT" value={metrics.csatAverage === null ? UNKNOWN : `${metrics.csatAverage.toFixed(2)} / 5`}
                tone="done" hint={`${metrics.csatResponses} 位客户评过`} />
            </div>

            <div className="grid sm:grid-cols-3 gap-3">
              <Small label="平均处理时长" value={metrics.avgHandleMinutes === null ? UNKNOWN : `${Math.round(metrics.avgHandleMinutes)} 分钟`} />
              <Small label="平均首次响应" value={metrics.avgFirstResponseMinutes === null ? UNKNOWN : `${Math.round(metrics.avgFirstResponseMinutes)} 分钟`} />
              <Small label="SLA 超期未闭" value={fmtInt(metrics.slaOverdueStillOpen)} alarm={metrics.slaOverdueStillOpen > 0} />
              <Small label="窗口内工单" value={fmtInt(metrics.total)} />
              <Small
                label="模型成本合计"
                value={metrics.llmCostTotal === null ? UNKNOWN : `¥${metrics.llmCostTotal.toFixed(4)}`}
                hint={metrics.unpricedTickets > 0 ? `另有 ${metrics.unpricedTickets} 张未定价` : undefined}
              />
              <Small label="再提被拒率" value={fmtRate(metrics.rejectedAttemptRate)} />
            </div>

            <section className="card-surface rounded-2xl p-5">
              <div className="label-eyebrow mb-3">操作台账 · 窗口内</div>
              <div className="flex flex-wrap gap-2">
                <Bar label="已执行" value={metrics.operations.executed} tone="var(--c-done)" total={barTotal(metrics)} />
                <Bar label="幂等重放" value={metrics.operations.replayed} tone="var(--c-active)" total={barTotal(metrics)} />
                <Bar label="失败" value={metrics.operations.failed} tone="var(--c-bad)" total={barTotal(metrics)} />
                <Bar label="被治理拦下" value={metrics.operations.blocked} tone="var(--c-hold)" total={barTotal(metrics)} />
                <Bar label="等人批" value={metrics.operations.pendingApproval} tone="var(--c-idle)" total={barTotal(metrics)} />
              </div>

              <div className="mt-4 pt-3 border-t border-line">
                <div className="label-eyebrow mb-2">判错之后的补救动作</div>
                  {Object.keys(metrics.wrongByCorrectiveAction).length === 0 ? (
                  <p className="text-[12px] text-ink-faint">
                    还没有"判错"的记录。这个空值的含义是"没人复核出错误"，
                    不等于"没有错误"——去治理台把待复核的那几笔看完再下结论。
                  </p>
                ) : (
                  <ul className="flex flex-col gap-1.5">
                    {Object.entries(metrics.wrongByCorrectiveAction).map(([action, count]) => (
                      <li key={action} className="flex items-center gap-2 text-[12px]">
                        <span className="min-w-0 flex-1 truncate text-ink">{action || "未填写动作"}</span>
                        <span className="num text-ink-soft">{count}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </section>

            {health && (
              <section className="card-surface rounded-2xl p-5">
                <div className="flex items-center gap-2 mb-3">
                  <Activity className="w-4 h-4 text-accent" />
                  <h2 className="text-[14px] font-semibold text-ink">线上健康</h2>
                  <span className="num text-[11px] text-ink-faint ml-auto">
                    窗口 {health.report.windowHours} 小时 · {fmtInt(health.report.totalTickets)} 张
                  </span>
                </div>
                {!health.enabled && (
                  <p className="text-[12px] text-ink-faint mb-2">
                    监控开关关着（MONITOR_ENABLED=false），下面的数字不会触发告警。
                  </p>
                )}
                {!health.report.sufficient ? (
                  <p className="text-[12px] text-ink-faint">
                    样本不足（{health.report.totalTickets} 张），不判阈值——
                    这时候报"健康"是没有依据的。
                  </p>
                ) : health.report.breaches.length === 0 ? (
                  <p className="text-[12px] text-state-done">全部阈值内。</p>
                ) : (
                  <ul className="flex flex-col gap-1.5">
                    {health.report.breaches.map((breach) => (
                      <li key={breach.metric} className="flex items-center gap-2 text-[12px] text-state-bad">
                        <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
                        <span className="min-w-0 flex-1">{LABEL_BY_METRIC[breach.metric] ?? breach.metric}</span>
                        <span className="num">{breach.value.toFixed(3)} &gt; {breach.threshold}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {health.alertsCreated > 0 && (
                  <p className="text-[11px] text-ink-faint mt-2">
                    这一次查看新建了 {health.alertsCreated} 条告警通知。
                  </p>
                )}
              </section>
            )}

            {usage && (
              <section className="card-surface rounded-2xl overflow-hidden">
                <div className="px-5 py-3.5 border-b border-line flex items-center gap-2">
                  <h2 className="text-[14px] font-semibold text-ink">模型用量</h2>
                  <span className="num text-[11px] text-ink-faint ml-auto">
                    {fmtInt(usage.totals.promptTokens)} + {fmtInt(usage.totals.completionTokens)} tokens ·{" "}
                    {usage.totals.promptCacheHitRate === null
                      ? "缓存命中未知"
                      : `缓存命中 ${fmtRate(usage.totals.promptCacheHitRate, 0)}`}
                  </span>
                </div>
                {usage.byModel.slice(0, 6).map((row) => (
                  <div key={row.model ?? "unknown"} className="px-5 py-2.5 border-b border-line last:border-0 flex items-center gap-3 text-[12px]">
                    <span className="num min-w-0 flex-1 truncate text-ink">{row.model ?? UNKNOWN}</span>
                    <span className="num text-ink-faint">{fmtInt(row.calls)} 次</span>
                    <span className="num text-ink-faint">{fmtMs(row.avgMs)}</span>
                    <span className="num w-[92px] text-right text-ink-soft">
                      {fmtCost(row.cost, row.currency)}
                    </span>
                    {row.failures > 0 && (
                      <span className="num text-state-bad">{row.failures} 失败</span>
                    )}
                  </div>
                ))}
                {!usage.pricingConfigured && (
                  <p className="px-5 py-2.5 text-[11px] text-ink-faint">
                    价目表未配置：成本列显示"未知"，因为一个猜出来的钱数比空白更危险。
                  </p>
                )}
              </section>
            )}
          </>
        )}
      </div>
    </div>
  );
};

const LABEL_BY_METRIC: Record<string, string> = {
  errorRate: "失败率",
  interventionRate: "人工介入率",
  avgCostPerTicket: "单工单平均成本",
  p95HandleMs: "p95 处理时长",
};

function barTotal(metrics: TicketMetrics): number {
  const { executed, replayed, failed, blocked, pendingApproval } = metrics.operations;
  return Math.max(1, executed + replayed + failed + blocked + pendingApproval);
}

const TONE_VAR: Record<string, string> = {
  done: "var(--c-done)",
  hold: "var(--c-hold)",
  bad: "var(--c-bad)",
  active: "var(--c-active)",
  idle: "var(--c-idle)",
};

const Big: React.FC<{ label: string; value: string; tone: string; hint?: string }> = ({
  label,
  value,
  tone,
  hint,
}) => (
  <div className="card-surface card-lift rounded-2xl p-4 flex flex-col gap-1.5">
    <div className="label-eyebrow">{label}</div>
    <div
      className="font-display text-[30px] font-semibold leading-none num"
      style={{ color: value === UNKNOWN ? "var(--c-ink-faint)" : TONE_VAR[tone] ?? "var(--c-ink)" }}
    >
      {value}
    </div>
    {hint && <div className="text-[10.5px] text-ink-faint leading-snug">{hint}</div>}
  </div>
);

const Small: React.FC<{ label: string; value: string; hint?: string; alarm?: boolean }> = ({
  label,
  value,
  hint,
  alarm,
}) => (
  <div
    className="rounded-xl border px-3.5 py-2.5 flex items-baseline justify-between gap-2"
    style={{
      borderColor: alarm ? "var(--c-line)" : "var(--c-line)",
      background: alarm ? "var(--c-bad-faint)" : "var(--c-surface)",
    }}
  >
    <span className="text-[11.5px] text-ink-soft">{label}</span>
    <span className="num text-[14px] font-semibold" style={{ color: alarm ? "var(--c-bad)" : "var(--c-ink)" }}>
      {value}
    </span>
    {hint && <span className="text-[10px] text-ink-faint shrink-0">{hint}</span>}
  </div>
);

const Bar: React.FC<{ label: string; value: number; tone: string; total: number }> = ({
  label,
  value,
  tone,
  total,
}) => (
  <div className="flex-1 min-w-[120px]">
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[11.5px] text-ink-soft">{label}</span>
      <span className="num text-[12.5px] text-ink">{value}</span>
    </div>
    <div className="gauge mt-1.5">
      <i style={{ width: `${(value / total) * 100}%`, background: tone }} />
    </div>
  </div>
);
