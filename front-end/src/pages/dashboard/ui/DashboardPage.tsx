import React, { useEffect, useState } from "react";
import { useSelector } from "react-redux";
import { useNavigate } from "react-router-dom";
import { RootState } from "@/app/providers/store";
import { apiClient } from "@/shared/api/client";
import type {
  UsageSummary,
  TraceSummary,
  ReviewLedgerItem,
} from "@/shared/types/api.types";
import { fmtCost, fmtInt, fmtMs } from "@/shared/lib/format";
import { PageHeader } from "@/shared/ui/PageHeader";
import { MetricCard } from "../components/MetricCard";
import { BarRow } from "../components/BarRow";
import { ShortcutCard } from "../components/ShortcutCard";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import {
  Loader2,
  ClipboardCheck,
  MessageSquare,
  BookOpen,
  ScrollText,
  Coins,
  Zap,
  MessagesSquare,
  Timer,
  ArrowRight,
  AlertTriangle,
} from "lucide-react";

const RANGES = [1, 7, 30] as const;

const VERDICT_META = {
  pass: { label: "通过", cls: "text-emerald-600 dark:text-emerald-400" },
  reject: { label: "不通过", cls: "text-rose-600 dark:text-rose-400" },
  needs_human: { label: "需要人判断", cls: "text-amber-600 dark:text-amber-400" },
} as const;

const hour = new Date().getHours();
const greeting =
  hour < 6 ? "夜深了" : hour < 12 ? "早上好" : hour < 18 ? "下午好" : "晚上好";

export const DashboardPage: React.FC = () => {
  const navigate = useNavigate();
  const { user } = useSelector((s: RootState) => s.auth);
  const toast = useToast();

  const [days, setDays] = useState(7);
  const [pending, setPending] = useState<ReviewLedgerItem[]>([]);
  const [ledger, setLedger] = useState<ReviewLedgerItem[]>([]);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [docCount, setDocCount] = useState<number | null>(null);
  const [skillCount, setSkillCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      apiClient.getReviews(true, 200),
      apiClient.getReviews(false, 200),
      apiClient.getUsage(days),
      apiClient.getTraces(undefined, 5),
      apiClient.getDocuments().catch(() => []),
      apiClient.getSkills().catch(() => null),
    ])
      .then(([p, all, u, t, docs, skills]) => {
        if (cancelled) return;
        setPending(p.items);
        setLedger(all.items);
        setUsage(u);
        setTraces(t);
        setDocCount(docs.length);
        setSkillCount(
          skills ? skills.builtin.length + skills.workspace.length : null,
        );
      })
      .catch((e) => {
        if (!cancelled) toast.error(toastMessageFrom(e, "加载工作台数据失败"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [days, toast]);

  const tally = { pass: 0, reject: 0, needs_human: 0 };
  ledger.forEach((r) => {
    tally[r.verdict] += 1;
  });
  const ledgerTotal = ledger.length || 1;

  const totals = usage?.totals;
  const cost = usage?.costs.length
    ? usage.costs.map((c) => fmtCost(c.amount, c.currency)).join(" / ")
    : "无数据";
  const promptHit =
    totals?.promptCacheHitRate != null
      ? Math.round(totals.promptCacheHitRate * 100)
      : null;

  return (
    <div className="page-shell app-atmosphere transition-colors duration-200">
      <div className="relative z-10 space-y-6 max-w-6xl">
        <PageHeader
          eyebrow="工作台"
          title={`${greeting}${user?.name ? `，${user.name}` : ""}`}
          description="此刻该关心什么：待办审核、审结走向、资产与用量，一屏看全。"
          actions={
            <div className="seg-switch">
              {RANGES.map((r) => (
                <button
                  key={r}
                  data-active={days === r}
                  onClick={() => setDays(r)}
                >
                  {r} 天
                </button>
              ))}
            </div>
          }
        />

        {loading && (
          <div className="flex items-center gap-2 text-xs text-[#6e6b63] dark:text-[#a19f96]">
            <Loader2 className="w-4 h-4 animate-spin" /> 正在汇总...
          </div>
        )}

        <div className="card-surface rounded-2xl p-6 anim-fade-up">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-center gap-3">
              <span className="w-11 h-11 rounded-xl bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
                <ClipboardCheck className="w-5 h-5" />
              </span>
              <div>
                <div className="label-eyebrow">待办审核</div>
                <div className="font-display text-[28px] font-semibold leading-tight text-[#1f1e1d] dark:text-[#edece8]">
                  {pending.length}
                  <span className="text-sm font-sans text-[#918d83] ml-1.5">
                    条需要人判断
                  </span>
                </div>
              </div>
            </div>
            {pending.length > 0 && (
              <button
                onClick={() => navigate("/reviews?filter=pending")}
                className="btn-accent px-4 py-2 rounded-xl text-white text-xs font-medium flex items-center gap-2"
              >
                <ArrowRight className="w-3.5 h-3.5" />
                去处理
              </button>
            )}
          </div>
          {pending.length === 0 ? (
            <p className="text-xs text-[#918d83] mt-4">
              没有等着人判断的结论。转人工的单据会先落在这里，别让它们堆进没人看的队列。
            </p>
          ) : (
            <div className="mt-4 space-y-0.5">
              {pending.slice(0, 4).map((it) => (
                <button
                  key={it.id}
                  onClick={() => navigate("/reviews?filter=pending")}
                  className="w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-xs text-left transition-colors"
                >
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0" />
                  <span className="flex-1 truncate text-[#1f1e1d] dark:text-[#edece8]">
                    {it.subject}
                  </span>
                  <span className="text-[#918d83] shrink-0">{it.sopName}</span>
                </button>
              ))}
              {pending.length > 4 && (
                <div className="text-[11px] text-[#918d83] px-3.5 pt-1">
                  还有 {pending.length - 4} 条…
                </div>
              )}
            </div>
          )}
        </div>

        {usage && (
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 anim-fade-up stagger-1">
            <MetricCard
              label="回答次数"
              value={fmtInt(totals!.turns)}
              icon={<MessagesSquare className="w-3.5 h-3.5" />}
            />
            <MetricCard
              label="成本"
              value={cost}
              icon={<Coins className="w-3.5 h-3.5" />}
            />
            <MetricCard
              label="上下文缓存"
              value={promptHit != null ? `${promptHit}%` : "暂无"}
              icon={<Zap className="w-3.5 h-3.5" />}
            />
            <MetricCard
              label="失败片段"
              value={fmtInt(totals!.failures)}
              icon={<Timer className="w-3.5 h-3.5" />}
              warn={totals!.failures > 0}
            />
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-5 gap-5 anim-fade-up stagger-2">
          <div className="lg:col-span-3 card-surface rounded-2xl p-5 space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="label-eyebrow">审结概览（近 {ledger.length} 条）</h3>
              <button
                onClick={() => navigate("/reviews?filter=all")}
                className="text-[11px] text-[#da7756] hover:underline"
              >
                全部台账 →
              </button>
            </div>
            {ledger.length === 0 ? (
              <p className="text-xs text-[#918d83]">
                还没有审核结论。让 Agent 按作业指导审一份材料，结论会记进台账。
              </p>
            ) : (
              <div className="space-y-2.5">
                {(["pass", "reject", "needs_human"] as const).map((v, i) => (
                  <BarRow
                    key={v}
                    label={VERDICT_META[v].label}
                    value={fmtInt(tally[v])}
                    pct={tally[v] / ledgerTotal}
                    delay={i * 0.06}
                  />
                ))}
              </div>
            )}
          </div>
          <div className="lg:col-span-2 grid grid-rows-2 gap-3">
            <button
              onClick={() => navigate("/knowledge")}
              className="card-surface card-lift rounded-2xl p-5 text-left flex items-center gap-3"
            >
              <span className="w-10 h-10 rounded-xl bg-[#da7756]/12 text-[#da7756] flex items-center justify-center shrink-0">
                <BookOpen className="w-5 h-5" />
              </span>
              <div>
                <div className="text-2xl font-semibold text-[#1f1e1d] dark:text-[#edece8] leading-none">
                  {docCount ?? "—"}
                </div>
                <div className="label-eyebrow mt-1">知识库文档</div>
              </div>
            </button>
            <button
              onClick={() => navigate("/skills")}
              className="card-surface card-lift rounded-2xl p-5 text-left flex items-center gap-3"
            >
              <span className="w-10 h-10 rounded-xl bg-[#da7756]/12 text-[#da7756] flex items-center justify-center shrink-0">
                <ScrollText className="w-5 h-5" />
              </span>
              <div>
                <div className="text-2xl font-semibold text-[#1f1e1d] dark:text-[#edece8] leading-none">
                  {skillCount ?? "—"}
                </div>
                <div className="label-eyebrow mt-1">作业指导</div>
              </div>
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 anim-fade-up stagger-3">
          <ShortcutCard
            icon={<MessageSquare className="w-4 h-4" />}
            title="开始一次审核"
            body="把材料交给 Agent，按作业指导逐项核对、给出有据结论。"
            onClick={() => navigate("/chat")}
          />
          <ShortcutCard
            icon={<ClipboardCheck className="w-4 h-4" />}
            title="审核台账"
            body="看结论依据、处置转人工的待办、追溯当时按的哪版 SOP。"
            onClick={() => navigate("/reviews")}
          />
          <ShortcutCard
            icon={<BookOpen className="w-4 h-4" />}
            title="知识库检索"
            body="不经过对话，直接看混合检索命中了什么。"
            onClick={() => navigate("/knowledge")}
          />
        </div>

        <div className="card-surface rounded-2xl p-5 space-y-3 anim-fade-up stagger-4">
          <div className="flex items-center justify-between">
            <h3 className="label-eyebrow">最近运行</h3>
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
                还没有埋点数据。去对话一次，这里会出现一条可回放的轨迹。
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
                {t.failures > 0 && (
                  <span className="text-rose-500">{t.failures} 失败</span>
                )}
                <span className="ml-auto text-[#da7756]">查看 →</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};




