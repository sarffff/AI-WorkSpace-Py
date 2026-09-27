import React, { useCallback, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { apiClient } from "@/shared/api/client";
import type {
  ReviewLedgerItem,
  ReviewVerdict,
} from "@/shared/types/api.types";
import { PageHeader } from "@/shared/ui/PageHeader";
import { EmptyState } from "@/shared/ui/EmptyState";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import {
  ClipboardCheck,
  Check,
  X,
  AlertTriangle,
  Loader2,
  Inbox,
  ShieldCheck,
} from "lucide-react";

/**
 * 审核台账：看结论、处置待办。
 *
 * 两个筛选不是便利功能而是这页存在的一半理由——needs_human 那一档如果没有"待办
 * 在哪"的入口，转人工就等于把结论扔进一个没人看的队列，那比直接给个错结论更难发现。
 * 所以默认落在「待办」上。
 */
type Tab = "pending" | "all";

const VERDICT_META: Record<
  ReviewVerdict,
  { label: string; cls: string; icon: React.ReactNode }
> = {
  pass: {
    label: "通过",
    cls: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/20",
    icon: <Check className="w-3.5 h-3.5" />,
  },
  reject: {
    label: "不通过",
    cls: "bg-rose-500/10 text-rose-600 dark:text-rose-400 border-rose-500/20",
    icon: <X className="w-3.5 h-3.5" />,
  },
  needs_human: {
    label: "需要人判断",
    cls: "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20",
    icon: <AlertTriangle className="w-3.5 h-3.5" />,
  },
};

const RESOLUTION_LABELS: Record<string, string> = {
  approved: "已批准",
  rejected: "已驳回",
  amended: "已修正",
};

const RESOLUTION_OPTIONS: { value: string; label: string }[] = [
  { value: "approved", label: "批准（结论成立）" },
  { value: "rejected", label: "驳回（结论不成立）" },
  { value: "amended", label: "修正（人工改了结论）" },
];

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

const VerdictBadge: React.FC<{ verdict: ReviewVerdict }> = ({ verdict }) => {
  const meta = VERDICT_META[verdict];
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold border ${meta.cls}`}
    >
      {meta.icon}
      {meta.label}
    </span>
  );
};

export const ReviewsPage: React.FC = () => {
  const toast = useToast();
  // 上下文面板的待办/全部筛选通过 ?filter= 深链进来，页面读它决定初始 tab
  const location = useLocation();
  const paramFilter = new URLSearchParams(location.search).get("filter");
  const [items, setItems] = useState<ReviewLedgerItem[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>(paramFilter === "all" ? "all" : "pending");

  // 深链参数变化时跟着切 tab（在图标轨/上下文面板点筛选时）
  useEffect(() => {
    if (paramFilter === "all" || paramFilter === "pending") {
      setTab(paramFilter);
    }
  }, [paramFilter]);

  // 正在复核的那一条（打开处置弹窗）
  const [resolving, setResolving] = useState<ReviewLedgerItem | null>(null);
  const [resolution, setResolution] = useState("approved");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const refresh = useCallback(
    (which: Tab) => {
      setLoading(true);
      setError(null);
      apiClient
        .getReviews(which === "pending")
        .then((res) => {
          setItems(res.items);
          setEnabled(res.enabled);
        })
        .catch((e) => setError(toastMessageFrom(e, "加载台账失败")))
        .finally(() => setLoading(false));
    },
    [],
  );

  useEffect(() => {
    refresh(tab);
  }, [tab, refresh]);

  const openResolve = (item: ReviewLedgerItem) => {
    setResolving(item);
    setResolution("approved");
    setNote("");
  };

  const submitResolve = async () => {
    if (!resolving) return;
    setSubmitting(true);
    try {
      await apiClient.resolveReview(resolving.id, resolution, note.trim());
      toast.success("已记下处置");
      setResolving(null);
      refresh(tab);
    } catch (e) {
      toast.error(toastMessageFrom(e, "处置失败"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="page-shell app-atmosphere transition-colors duration-200">
      <div className="relative z-10 space-y-6 max-w-5xl">
        <PageHeader
          eyebrow="审核"
          title="审核台账"
          description="Agent 审完记进来的结论——看依据、追溯当时按的哪一版 SOP、处置转人工的待办。"
          actions={
            <button
              onClick={() => refresh(tab)}
              className="px-4 py-2.5 text-xs font-medium rounded-xl bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#201f1c] dark:hover:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] border border-[#e3dfd5] dark:border-[#2e2d2a]"
            >
              刷新
            </button>
          }
        />

        <div className="seg-switch w-fit relative z-10">
          {(
            [
              ["pending", "待办"],
              ["all", "全部"],
            ] as [Tab, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              data-active={tab === key}
              onClick={() => setTab(key)}
            >
              {label}
            </button>
          ))}
        </div>

        {error && (
          <div className="flex items-center justify-between gap-3 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs">
            <span>{error}</span>
            <button onClick={() => setError(null)} className="p-0.5">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}

        {!enabled ? (
          <EmptyState
            icon={<ShieldCheck className="w-12 h-12 text-[#da7756]" />}
            title="审核台账未开启"
            description="后端 REVIEW_LEDGER_ENABLED 关着。开启后，Agent 按作业指导审完的结论会结构化记到这里，而不是只写在回答正文里。"
          />
        ) : loading ? (
          <div className="flex justify-center py-16 text-[#918d83]">
            <Loader2 className="w-6 h-6 animate-spin" />
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            icon={<Inbox className="w-12 h-12 text-[#da7756]" />}
            title={tab === "pending" ? "没有待办" : "台账还是空的"}
            description={
              tab === "pending"
                ? "没有等着人判断的结论。转人工的单据会落在这里，别让它们堆进没人看的队列。"
                : "Agent 还没有把任何审核结论记进台账。"
            }
          />
        ) : (
          <div className="space-y-4">
            {items.map((item) => (
              <ReviewCard key={item.id} item={item} onResolve={openResolve} />
            ))}
          </div>
        )}
      </div>
      {resolving && (
        <div
          className="fixed inset-0 bg-black/45 backdrop-blur-sm flex items-center justify-center z-50 p-4"
          onClick={() => !submitting && setResolving(null)}
        >
          <div
            className="bg-[#fbf9f5] dark:bg-[#1a1917] rounded-2xl border border-[#e6e2d8] dark:border-[#282724] shadow-2xl w-full max-w-lg anim-fade-up"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between p-5 border-b border-[#e6e2d8] dark:border-[#282724]">
              <h3 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8] flex items-center gap-2 min-w-0">
                <ClipboardCheck className="w-4 h-4 text-[#da7756] shrink-0" />
                <span className="truncate">复核：{resolving.subject}</span>
              </h3>
              <button
                onClick={() => !submitting && setResolving(null)}
                className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[#6e6b63] shrink-0"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-5 space-y-4">
              <div className="flex items-center gap-2 text-xs text-[#6e6b63] dark:text-[#a19f96]">
                <span>Agent 结论</span>
                <VerdictBadge verdict={resolving.verdict} />
              </div>
              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                  处置
                </label>
                <select
                  value={resolution}
                  onChange={(e) => setResolution(e.target.value)}
                  className="w-full bg-white dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-3.5 py-2.5 text-xs text-[#1f1e1d] dark:text-[#edece8]"
                >
                  {RESOLUTION_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                  备注（可选）
                </label>
                <textarea
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  rows={3}
                  className="w-full bg-white dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-3.5 py-2.5 text-xs text-[#1f1e1d] dark:text-[#edece8] resize-y"
                  placeholder="为什么这么判——留给下一个看台账的人。复核过就不能再改了。"
                />
              </div>
            </div>
            <div className="flex justify-end gap-2 p-5 border-t border-[#e6e2d8] dark:border-[#282724]">
              <button
                onClick={() => setResolving(null)}
                disabled={submitting}
                className="px-4 py-2 text-xs font-medium rounded-xl bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#201f1c] dark:hover:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] border border-[#e3dfd5] dark:border-[#2e2d2a] disabled:opacity-50"
              >
                取消
              </button>
              <button
                onClick={submitResolve}
                disabled={submitting}
                className="btn-accent px-4 py-2 text-xs font-medium rounded-xl text-white disabled:opacity-50 flex items-center gap-2"
              >
                {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                记下处置
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

interface ReviewCardProps {
  item: ReviewLedgerItem;
  onResolve: (item: ReviewLedgerItem) => void;
}

const ReviewCard: React.FC<ReviewCardProps> = ({ item, onResolve }) => {
  const resolved = !!item.resolvedAt;
  // runs=1 读作"没做一致性检查"，不是"检查过且一致"——台账上这两者必须分得开
  const consensusNote =
    item.runs > 1 ? `复审 ${item.runs} 次一致` : "未做独立复审";
  return (
    <div className="card-surface p-5 rounded-2xl space-y-4 anim-fade-up">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h3 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8] truncate">
            {item.subject}
          </h3>
          <div className="text-[11px] text-[#918d83]">
            按 {item.sopName} 第 {item.sopVersion} 版 · {formatDate(item.createdAt)} ·{" "}
            {consensusNote}
          </div>
        </div>
        <VerdictBadge verdict={item.verdict} />
      </div>
      {item.inputs.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {item.inputs.map((inp, i) => (
            <span
              key={i}
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-[11px] border ${
                inp.found
                  ? "bg-emerald-500/5 text-emerald-700 dark:text-emerald-400 border-emerald-500/20"
                  : "bg-rose-500/5 text-rose-600 dark:text-rose-400 border-rose-500/20"
              }`}
              title={inp.value ?? ""}
            >
              {inp.found ? (
                <Check className="w-3 h-3" />
              ) : (
                <X className="w-3 h-3" />
              )}
              {inp.name}
            </span>
          ))}
        </div>
      )}

      {item.basis.length > 0 && (
        <ul className="text-xs text-[#6e6b63] dark:text-[#a19f96] space-y-1 list-disc pl-4">
          {item.basis.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      )}

      {item.evidence && (
        <details className="text-xs group">
          <summary className="cursor-pointer text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8] select-none">
            依据原文
          </summary>
          <pre className="mt-2 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-[#52504a] dark:text-[#b0aea5] bg-[#f3f0e6] dark:bg-[#201f1c] p-3 rounded-xl border border-[#e6e2d8] dark:border-[#282724]">
            {item.evidence}
          </pre>
        </details>
      )}

      <div className="flex items-center justify-between gap-3 pt-3 border-t border-[#e6e2d8]/60 dark:border-[#282724]/60">
        {resolved ? (
          <div className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] min-w-0 truncate">
            {RESOLUTION_LABELS[item.resolution ?? ""] ?? item.resolution} ·{" "}
            {item.resolvedBy} · {formatDate(item.resolvedAt)}
            {item.resolutionNote ? ` · ${item.resolutionNote}` : ""}
          </div>
        ) : (
          <div className="text-[11px] text-[#918d83]">
            {item.verdict === "needs_human" ? "等待人判断" : "未复核"}
          </div>
        )}
        {!resolved && (
          <button
            onClick={() => onResolve(item)}
            className="btn-accent px-3 py-1.5 text-white text-[11px] font-medium rounded-lg shrink-0"
          >
            复核
          </button>
        )}
      </div>
    </div>
  );
};

