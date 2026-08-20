import React, { useEffect, useState } from "react";
import { useSelector } from "react-redux";
import { useNavigate } from "react-router-dom";
import { RootState } from "@/app/providers/store";
import { apiClient } from "@/shared/api/client";
import type { UsageSummary, TraceSummary } from "@/shared/types/api.types";
import { fmtCost, fmtInt, fmtMs, spanLabel } from "@/shared/lib/format";
import { PageHeader } from "@/shared/ui/PageHeader";
import { CapabilityStrip } from "@/shared/ui/CapabilityStrip";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import {
  Activity,
  Coins,
  Loader2,
  Timer,
  MessageSquare,
  Cpu,
  Sparkles,
  Zap,
  ShieldCheck,
  BookOpen,
  Route as RouteIcon,
} from "lucide-react";
import { AgentMetricsPanel } from "@/widgets/agent-metrics";
import { AgentLoopPanel } from "../components/AgentLoopPanel";
import { MetricCard } from "../components/MetricCard";
import { BarRow } from "../components/BarRow";
import { ShortcutCard } from "../components/ShortcutCard";

const RANGES = [1, 7, 30] as const;

const hour = new Date().getHours();
const greeting =
  hour < 6 ? "夜深了" : hour < 12 ? "早上好" : hour < 18 ? "下午好" : "晚上好";

export const DashboardPage: React.FC = () => {
  const navigate = useNavigate();
  const { serverStatus, selectedModel, sessions } = useSelector(
    (state: RootState) => state.chat,
  );
  const { user } = useSelector((state: RootState) => state.auth);

  const [days, setDays] = useState(7);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const toast = useToast();

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([apiClient.getUsage(days), apiClient.getTraces(undefined, 6)])
      .then(([u, t]) => {
        if (cancelled) return;
        setUsage(u);
        setTraces(t);
      })
      .catch((e) => {
        if (!cancelled) toast.error(toastMessageFrom(e, "加载仪表盘数据失败"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [days, toast]);

  const maxCalls = usage ? Math.max(...usage.byName.map((r) => r.calls), 1) : 1;
  const maxModelCalls = usage
    ? Math.max(...usage.byModel.map((r) => r.calls), 1)
    : 1;

  const cache = usage?.cache;
  const hitPct =
    cache?.hitRate != null ? Math.round(cache.hitRate * 100) : null;

  return (
    <div className="page-shell app-atmosphere transition-colors duration-200">
      <div className="relative z-10 space-y-7 max-w-6xl">
        <PageHeader
          eyebrow="Workbench"
          title={`${greeting}${user?.name ? `，${user.name}` : ""}`}
          description="看见模型怎么想——用量、轨迹、缓存与护栏都摊在台面上。"
          actions={
            <div className="seg-switch">
              {RANGES.map((range) => (
                <button
                  key={range}
                  data-active={days === range}
                  onClick={() => setDays(range)}
                >
                  {range} 天
                </button>
              ))}
            </div>
          }
        />

        <CapabilityStrip className="anim-fade-up stagger-1" />

        <AgentLoopPanel
          serverStatus={serverStatus}
          selectedModel={selectedModel}
          sessionCount={sessions.length}
          onChat={() => navigate("/chat")}
          onKnowledge={() => navigate("/knowledge")}
          onTraces={() => navigate("/traces")}
        />

        {loading && (
          <div className="flex items-center gap-2 text-xs text-[#6e6b63] dark:text-[#a19f96]">
            <Loader2 className="w-4 h-4 animate-spin" /> 正在统计...
          </div>
        )}

        {usage && !loading && (
          <>
            <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-3 anim-fade-up stagger-1">
              <MetricCard
                label="回答次数"
                value={fmtInt(usage.totals.turns)}
                icon={<MessageSquare className="w-3.5 h-3.5" />}
              />
              <MetricCard
                label="输入 token"
                value={fmtInt(usage.totals.promptTokens)}
                icon={<Activity className="w-3.5 h-3.5" />}
              />
              <MetricCard
                label="输出 token"
                value={fmtInt(usage.totals.completionTokens)}
                icon={<Sparkles className="w-3.5 h-3.5" />}
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
                icon={<Coins className="w-3.5 h-3.5" />}
              />
              <MetricCard
                label="缓存命中"
                value={
                  !cache?.enabled
                    ? "未启用"
                    : hitPct == null
                      ? "尚无查询"
                      : `${hitPct}%`
                }
                icon={<Zap className="w-3.5 h-3.5" />}
                hint={
                  cache?.enabled
                    ? `省 ${fmtInt(cache.tokensSaved)} tok`
                    : undefined
                }
              />
              <MetricCard
                label="失败片段"
                value={fmtInt(usage.totals.failures)}
                icon={<Timer className="w-3.5 h-3.5" />}
                warn={usage.totals.failures > 0}
              />
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-5 gap-5 anim-fade-up stagger-2">
              <div className="lg:col-span-3 card-surface rounded-2xl p-5 space-y-3">
                <h3 className="label-eyebrow">按环节</h3>
                <div className="space-y-2.5">
                  {usage.byName.map((row, i) => (
                    <BarRow
                      key={row.name ?? "unknown"}
                      label={spanLabel(row.name ?? "-")}
                      value={fmtInt(row.calls)}
                      pct={row.calls / maxCalls}
                      delay={i * 0.06}
                    />
                  ))}
                  {usage.byName.length === 0 && (
                    <p className="text-xs text-[#918d83]">暂无数据</p>
                  )}
                </div>
              </div>
              <div className="lg:col-span-2 card-surface rounded-2xl p-5 space-y-3">
                <h3 className="label-eyebrow">按模型</h3>
                <div className="space-y-2.5">
                  {usage.byModel.map((row, i) => (
                    <BarRow
                      key={row.model ?? "unknown"}
                      label={row.model ?? "-"}
                      value={`${fmtInt(row.calls)}次 · ${fmtCost(row.cost, row.currency)}`}
                      pct={row.calls / maxModelCalls}
                      delay={i * 0.06}
                    />
                  ))}
                  {usage.byModel.length === 0 && (
                    <p className="text-xs text-[#918d83]">暂无数据</p>
                  )}
                </div>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 anim-fade-up stagger-3">
              <ShortcutCard
                icon={<BookOpen className="w-4 h-4" />}
                title="检索调试"
                body="不经过对话，直接看 dense / sparse 命中了什么。"
                onClick={() => navigate("/knowledge")}
              />
              <ShortcutCard
                icon={<RouteIcon className="w-4 h-4" />}
                title="运行回放"
                body="把一次回答拆成 span 瀑布，核对耗时与失败。"
                onClick={() => navigate("/traces")}
              />
              <ShortcutCard
                icon={<Sparkles className="w-4 h-4" />}
                title="提示词实验"
                body="对比系统提示词版本，挂到下一轮对话上。"
                onClick={() => navigate("/prompts")}
              />
            </div>

            <div className="card-surface rounded-2xl p-5 space-y-3 anim-fade-up stagger-4">
              <div className="flex items-center justify-between">
                <h3 className="label-eyebrow">最近的回答</h3>
                <button
                  onClick={() => navigate("/traces")}
                  className="text-[11px] text-[#da7756] hover:underline"
                >
                  全部轨迹 →
                </button>
              </div>
              <div className="space-y-1">
                {traces.length === 0 && (
                  <p className="text-xs text-[#918d83] py-6 text-center">
                    还没有埋点数据。去对话一次，这里就会出现一条可回放的轨迹。
                  </p>
                )}
                {traces.map((t) => (
                  <button
                    key={t.traceId}
                    onClick={() => navigate(`/traces?trace=${t.traceId}`)}
                    className="w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-xs text-left transition-colors"
                  >
                    <span
                      className={`w-2 h-2 rounded-full shrink-0 ${
                        t.failures > 0 ? "bg-rose-500" : "bg-emerald-500"
                      }`}
                    />
                    <span className="text-[#6e6b63] dark:text-[#a19f96] w-36 shrink-0">
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
                    <span className="ml-auto text-[#da7756]">查看 →</span>
                  </button>
                ))}
              </div>
            </div>
          </>
        )}

        <div className="flex items-center gap-3 px-4 py-3 rounded-2xl bg-[#f3f0e6]/50 dark:bg-[#1e1d1b]/50 border border-[#e3dfd5] dark:border-[#2e2d2a] text-xs anim-fade-up stagger-5">
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
          {cache?.enabled && (
            <>
              <span className="text-[#918d83]">·</span>
              <Zap className="w-3.5 h-3.5 text-[#da7756]" />
              <span className="text-[#6e6b63] dark:text-[#a19f96]">
                语义缓存 {hitPct == null ? "待命" : `${hitPct}%`}
              </span>
            </>
          )}
          <span className="text-[#918d83]">·</span>
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-500" />
          <span className="text-[#6e6b63] dark:text-[#a19f96]">护栏就绪</span>
          <button
            onClick={() => navigate("/chat")}
            className="ml-auto flex items-center gap-1.5 btn-accent px-3 py-1.5 rounded-lg text-[11px] font-medium"
          >
            <MessageSquare className="w-3.5 h-3.5" />
            去对话
          </button>
        </div>

        <AgentMetricsPanel />
      </div>
    </div>
  );
};

