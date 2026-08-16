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
  GitBranch,
  Search,
  BrainCircuit,
} from "lucide-react";

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
      </div>
    </div>
  );
};

const AgentLoopPanel: React.FC<{
  serverStatus: string;
  selectedModel: string;
  sessionCount: number;
  onChat: () => void;
  onKnowledge: () => void;
  onTraces: () => void;
}> = ({
  serverStatus,
  selectedModel,
  sessionCount,
  onChat,
  onKnowledge,
  onTraces,
}) => {
  const online = serverStatus === "online";
  const stages = [
    {
      label: "路由",
      hint: "理解意图",
      icon: <GitBranch className="w-3.5 h-3.5" />,
      action: onChat,
    },
    {
      label: "检索",
      hint: "混合召回",
      icon: <Search className="w-3.5 h-3.5" />,
      action: onKnowledge,
    },
    {
      label: "推理",
      hint: selectedModel,
      icon: <BrainCircuit className="w-3.5 h-3.5" />,
      action: onChat,
    },
    {
      label: "护栏",
      hint: "输出校验",
      icon: <ShieldCheck className="w-3.5 h-3.5" />,
      action: onTraces,
    },
  ];

  return (
    <section className="card-surface rounded-2xl p-4 sm:p-5 anim-fade-up stagger-2 overflow-hidden relative">
      <div className="absolute inset-y-0 right-0 w-1/3 pointer-events-none opacity-40 bg-[radial-gradient(circle_at_70%_20%,rgba(218,119,86,0.14),transparent_65%)]" />
      <div className="relative flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1.5">
            <span className={`relative flex w-2 h-2 ${online ? "pulse-ring" : ""}`}>
              <span className={`relative inline-flex rounded-full h-2 w-2 ${online ? "bg-emerald-500" : "bg-amber-500"}`} />
            </span>
            <span className="label-eyebrow">Agent loop</span>
            <span className="chip text-[9px] px-1.5 py-0.5">{online ? "可运行" : "等待服务"}</span>
          </div>
          <h2 className="font-display text-base font-semibold text-[#1f1e1d] dark:text-[#edece8]">
            一次回答，四个可观测阶段
          </h2>
          <p className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] mt-1 max-w-md leading-relaxed">
            从意图路由到检索、模型推理，再到安全校验。每一步都能单独跳转查看证据。
          </p>
        </div>

        <div className="flex items-center gap-2.5 sm:gap-3 overflow-x-auto pb-1 lg:pb-0 lg:min-w-[31rem]">
          {stages.map((stage, index) => (
            <React.Fragment key={stage.label}>
              <button
                type="button"
                onClick={stage.action}
                className="group flex min-w-[5.5rem] flex-col items-center gap-1.5 rounded-xl px-2 py-2 transition-colors hover:bg-[#f3f0e6] dark:hover:bg-[#262522]"
              >
                <span className="flex h-8 w-8 items-center justify-center rounded-xl border border-[#e3dfd5] bg-[#fbf9f5] text-[#da7756] shadow-sm transition-transform group-hover:-translate-y-0.5 dark:border-[#33312d] dark:bg-[#1e1d1b]">
                  {stage.icon}
                </span>
                <span className="text-[11px] font-semibold text-[#1f1e1d] dark:text-[#edece8]">{stage.label}</span>
                <span className="max-w-[6.5rem] truncate text-[9px] text-[#918d83]" title={stage.hint}>{stage.hint}</span>
              </button>
              {index < stages.length - 1 && <span className="h-px w-4 shrink-0 bg-[#dcd7cb] dark:bg-[#33312d]" />}
            </React.Fragment>
          ))}
        </div>

        <div className="hidden shrink-0 border-l border-[#e6e2d8] pl-4 text-right dark:border-[#2a2926] lg:block">
          <div className="label-eyebrow mb-1">Active context</div>
          <div className="font-mono text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">{sessionCount}</div>
          <div className="text-[10px] text-[#918d83]">个会话</div>
        </div>
      </div>
    </section>
  );
};

const MetricCard: React.FC<{
  label: string;
  value: string;
  icon: React.ReactNode;
  warn?: boolean;
  hint?: string;
}> = ({ label, value, icon, warn, hint }) => (
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
    {hint && <div className="text-[10px] text-[#918d83]">{hint}</div>}
  </div>
);

const BarRow: React.FC<{
  label: string;
  value: string;
  pct: number;
  delay?: number;
}> = ({ label, value, pct, delay = 0 }) => (
  <div className="flex items-center gap-3 text-[11px]">
    <span className="w-28 shrink-0 truncate text-[#1f1e1d] dark:text-[#edece8]">
      {label}
    </span>
    <div className="flex-1 h-2 rounded-full bg-[#f3f0e6] dark:bg-[#262522] overflow-hidden">
      <div
        className="h-full rounded-full bg-gradient-to-r from-[#da7756] to-[#e0845f] anim-bar"
        style={{
          width: `${Math.min(pct * 100, 100)}%`,
          animationDelay: `${delay}s`,
        }}
      />
    </div>
    <span className="w-28 text-right text-[#6e6b63] dark:text-[#a19f96] truncate">
      {value}
    </span>
  </div>
);

const ShortcutCard: React.FC<{
  icon: React.ReactNode;
  title: string;
  body: string;
  onClick: () => void;
}> = ({ icon, title, body, onClick }) => (
  <button
    onClick={onClick}
    className="card-surface card-lift rounded-2xl p-4 text-left space-y-2"
  >
    <div className="w-8 h-8 rounded-xl bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
      {icon}
    </div>
    <div className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
      {title}
    </div>
    <p className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] leading-relaxed">
      {body}
    </p>
  </button>
);
