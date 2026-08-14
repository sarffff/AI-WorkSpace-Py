import React, { useEffect, useState } from "react";
import { useSelector } from "react-redux";
import { useNavigate } from "react-router-dom";
import { RootState } from "@/app/providers/store";
import { apiClient } from "@/shared/api/client";
import type { UsageSummary, TraceSummary } from "@/shared/types/api.types";
import { fmtCost, fmtInt, fmtMs, spanLabel } from "@/shared/lib/format";
import {
  Activity,
  Coins,
  Loader2,
  Timer,
  MessageSquare,
  Cpu,
  Database,
  Sparkles,
} from "lucide-react";

const RANGES = [1, 7, 30] as const;

export const DashboardPage: React.FC = () => {
  const navigate = useNavigate();
  const { serverStatus, selectedModel } = useSelector(
    (state: RootState) => state.chat,
  );

  const [days, setDays] = useState(7);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([apiClient.getUsage(days), apiClient.getTraces(undefined, 6)])
      .then(([u, t]) => {
        if (cancelled) return;
        setUsage(u);
        console.log(u);
        setTraces(t);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [days]);

  const maxCalls = usage ? Math.max(...usage.byName.map((r) => r.calls), 1) : 1;
  const maxModelCalls = usage
    ? Math.max(...usage.byModel.map((r) => r.calls), 1)
    : 1;

  return (
    <div className="p-8 h-full overflow-y-auto app-atmosphere transition-colors duration-200">
      <div className="relative z-10 space-y-6">
        {/* 顶栏:问候 + days 切换 */}
        <div className="flex items-center justify-between anim-fade-up">
          <div>
            <h1 className="font-display text-[26px] font-semibold text-[#1f1e1d] dark:text-[#edece8]">
              工作台
            </h1>
            <p className="text-xs text-[#6e6b63] dark:text-[#a19f96] mt-1">
              运行概览与用量统计
            </p>
          </div>
          <div className="flex gap-1.5">
            {RANGES.map((range) => (
              <button
                key={range}
                onClick={() => setDays(range)}
                className={`px-3 py-1.5 text-[11px] rounded-lg transition-colors ${
                  days === range
                    ? "bg-[#da7756] text-white"
                    : "bg-[#f3f0e6] dark:bg-[#262522] text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
                }`}
              >
                {range} 天
              </button>
            ))}
          </div>
        </div>

        {loading && (
          <div className="flex items-center gap-2 text-xs text-[#6e6b63] dark:text-[#a19f96]">
            <Loader2 className="w-4 h-4 animate-spin" /> 正在统计...
          </div>
        )}

        {usage && !loading && (
          <>
            {/* 指标卡 */}
            <div className="grid grid-cols-5 gap-4 anim-fade-up stagger-1">
              <MetricCard
                label="回答次数"
                value={fmtInt(usage.totals.turns)}
                icon={<MessageSquare className="w-4 h-4" />}
              />
              <MetricCard
                label="输入 token"
                value={fmtInt(usage.totals.promptTokens)}
                icon={<Activity className="w-4 h-4" />}
              />
              <MetricCard
                label="输出 token"
                value={fmtInt(usage.totals.completionTokens)}
                icon={<Sparkles className="w-4 h-4" />}
              />
              <MetricCard
                label="成本"
                value={
                  usage.costs.length
                    ? usage.costs
                        .map((c) => fmtCost(c.amount, c.currency))
                        .join(" / ")
                    : "无数据"
                }
                icon={<Coins className="w-4 h-4" />}
              />
              <MetricCard
                label="失败片段"
                value={fmtInt(usage.totals.failures)}
                icon={<Timer className="w-4 h-4" />}
                warn={usage.totals.failures > 0}
              />
            </div>

            {/* 双栏:按环节 + 按模型 */}
            <div className="grid grid-cols-2 gap-5 anim-fade-up stagger-2">
              <div className="card-surface rounded-2xl p-5 space-y-3">
                <h3 className="label-eyebrow">按环节</h3>
                <div className="space-y-2.5">
                  {usage.byName.map((row) => (
                    <BarRow
                      key={row.name ?? "unknown"}
                      label={spanLabel(row.name ?? "-")}
                      value={fmtInt(row.calls)}
                      pct={row.calls / maxCalls}
                      suffix="次"
                    />
                  ))}
                  {usage.byName.length === 0 && (
                    <p className="text-xs text-[#918d83]">暂无数据</p>
                  )}
                </div>
              </div>
              <div className="card-surface rounded-2xl p-5 space-y-3">
                <h3 className="label-eyebrow">按模型</h3>
                <div className="space-y-2.5">
                  {usage.byModel.map((row) => (
                    <BarRow
                      key={row.model ?? "unknown"}
                      label={row.model ?? "-"}
                      value={`${fmtInt(row.calls)}次 · ${fmtCost(row.cost, row.currency)}`}
                      pct={row.calls / maxModelCalls}
                    />
                  ))}
                  {usage.byModel.length === 0 && (
                    <p className="text-xs text-[#918d83]">暂无数据</p>
                  )}
                </div>
              </div>
            </div>

            {/* 最近的回答 */}
            <div className="card-surface rounded-2xl p-5 space-y-3 anim-fade-up stagger-3">
              <h3 className="label-eyebrow">最近的回答</h3>
              <div className="space-y-1">
                {traces.length === 0 && (
                  <p className="text-xs text-[#918d83]">还没有埋点数据</p>
                )}
                {traces.map((t) => (
                  <button
                    key={t.traceId}
                    onClick={() => navigate(`/traces?trace=${t.traceId}`)}
                    className="w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl bg-[#faf9f5] dark:bg-[#191817] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-xs text-left transition-colors"
                  >
                    <span
                      className={`w-2 h-2 rounded-full shrink-0 ${
                        t.failures > 0 ? "bg-rose-500" : "bg-emerald-500"
                      }`}
                    />
                    <span className="text-[#6e6b63] dark:text-[#a19f96]">
                      {t.startedAt
                        ? new Date(t.startedAt).toLocaleString()
                        : t.traceId.slice(0, 8)}
                    </span>
                    <span className="flex items-center gap-1 text-[#1f1e1d] dark:text-[#edece8]">
                      <Timer className="w-3 h-3" /> {fmtMs(t.durationMs)}
                    </span>
                    <span className="text-[#6e6b63] dark:text-[#a19f96]">
                      {fmtInt(t.promptTokens + t.completionTokens)} tok
                    </span>
                    <span className="text-[#6e6b63] dark:text-[#a19f96]">
                      {fmtCost(t.cost, t.currency)}
                    </span>
                    {t.failures > 0 && (
                      <span className="text-rose-500">{t.failures} 失败</span>
                    )}
                    <span className="ml-auto text-[#da7756]">查看轨迹 →</span>
                  </button>
                ))}
              </div>
            </div>
          </>
        )}

        {/* 服务健康行 */}
        <div className="flex items-center gap-3 px-4 py-3 rounded-2xl bg-[#f3f0e6]/50 dark:bg-[#1e1d1b]/50 border border-[#e3dfd5] dark:border-[#2e2d2a] text-xs anim-fade-up stagger-4">
          <span className="relative flex w-2 h-2 shrink-0">
            {serverStatus === "online" && (
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-50" />
            )}
            <span
              className={`relative inline-flex rounded-full h-2 w-2 ${
                serverStatus === "online"
                  ? "bg-emerald-500"
                  : serverStatus === "offline"
                    ? "bg-rose-500"
                    : "bg-amber-500"
              }`}
            />
          </span>
          <span className="text-[#6e6b63] dark:text-[#a19f96]">
            {serverStatus === "online"
              ? "在线"
              : serverStatus === "offline"
                ? "离线"
                : "检查中"}
          </span>
          <span className="text-[#918d83]">·</span>
          <Cpu className="w-3.5 h-3.5 text-[#da7756]" />
          <span className="text-[#1f1e1d] dark:text-[#edece8] font-medium">
            {selectedModel}
          </span>
          <span className="text-[#918d83]">·</span>
          <Database className="w-3.5 h-3.5 text-emerald-500" />
          <span className="text-[#6e6b63] dark:text-[#a19f96]">
            MySQL + Redis
          </span>
          <button
            onClick={() => navigate("/chat")}
            className="ml-auto flex items-center gap-1.5 btn-accent px-3 py-1.5 rounded-lg text-[11px] font-medium"
          >
            <MessageSquare className="w-3.5 h-3.5" />
            去对话
          </button>
        </div>
      </div>
    </div>
  );
};

/* ---------- 子组件 ---------- */

const MetricCard: React.FC<{
  label: string;
  value: string;
  icon: React.ReactNode;
  warn?: boolean;
}> = ({ label, value, icon, warn }) => (
  <div className="card-surface card-lift rounded-2xl p-4 space-y-2">
    <div className="flex items-center gap-1.5 text-[10px] font-semibold tracking-wide text-[#6e6b63] dark:text-[#a19f96]">
      <span className={warn ? "text-rose-500" : "text-[#da7756]"}>{icon}</span>
      {label}
    </div>
    <div
      className={`text-xl font-bold leading-none ${
        warn ? "text-rose-500" : "text-[#1f1e1d] dark:text-[#edece8]"
      }`}
      style={{ fontFamily: "var(--font-mono)" }}
    >
      {value}
    </div>
  </div>
);

const BarRow: React.FC<{
  label: string;
  value: string;
  pct: number;
  suffix?: string;
}> = ({ label, value, pct }) => (
  <div className="flex items-center gap-3 text-[11px]">
    <span className="w-28 shrink-0 truncate text-[#1f1e1d] dark:text-[#edece8]">
      {label}
    </span>
    <div className="flex-1 h-4 rounded-full bg-[#f3f0e6] dark:bg-[#262522] overflow-hidden">
      <div
        className="h-full rounded-full bg-gradient-to-r from-[#da7756] to-[#e0845f] transition-all"
        style={{ width: `${Math.min(pct * 100, 100)}%` }}
      />
    </div>
    <span className="w-28 text-right text-[#6e6b63] dark:text-[#a19f96] truncate">
      {value}
    </span>
  </div>
);
