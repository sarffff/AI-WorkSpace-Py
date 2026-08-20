import React, { useEffect, useState, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { apiClient } from "@/shared/api/client";
import type {
  TraceSummary,
  TraceDetail,
} from "@/shared/types/api.types";
import { fmtCost, fmtInt, fmtMs } from "@/shared/lib/format";
import { PageHeader } from "@/shared/ui/PageHeader";
import { EmptyState } from "@/shared/ui/EmptyState";
import { BrandMark } from "@/shared/ui/BrandMark";
import {
  Loader2,
  RefreshCw,
  Timer,
  AlertCircle,
} from "lucide-react";
import { SpanWaterfall } from "../components/SpanWaterfall";

export const TracesPage: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(20);
  const [failuresOnly, setFailuresOnly] = useState(false);
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [traceDetail, setTraceDetail] = useState<TraceDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // 从 URL 参数读取预选 trace
  const traceParam = searchParams.get("trace");
  const chatParam = searchParams.get("chat");
  const messageParam = searchParams.get("message");

  const fetchTraces = useCallback(() => {
    setLoading(true);
    setError(null);
    apiClient
      .getTraces(chatParam || undefined, limit)
      .then((list) => {
        setTraces(list);
        // 按 messageId 匹配
        if (messageParam) {
          const found = list.find((t) => t.messageId === messageParam);
          if (found) {
            setSelectedTraceId(found.traceId);
          }
        }
      })
      .catch((e) => setError(e instanceof Error ? e.message : "加载失败"))
      .finally(() => setLoading(false));
  }, [chatParam, limit, messageParam]);

  useEffect(() => {
    fetchTraces();
  }, [fetchTraces]);

  // 选中 trace 时取详情
  useEffect(() => {
    if (!selectedTraceId) {
      setTraceDetail(null);
      return;
    }
    setDetailLoading(true);
    apiClient
      .getTrace(selectedTraceId)
      .then(setTraceDetail)
      .catch(() => setTraceDetail(null))
      .finally(() => setDetailLoading(false));
  }, [selectedTraceId]);

  // 从 URL 参数预选(如果列表里能匹配到)
  useEffect(() => {
    if (traceParam && traces.length > 0) {
      const found = traces.find((t) => t.traceId === traceParam);
      if (found) setSelectedTraceId(traceParam);
    }
  }, [traceParam, traces]);

  const displayed = failuresOnly
    ? traces.filter((t) => t.failures > 0)
    : traces;

  return (
    <div className="page-shell app-atmosphere transition-colors duration-200">
      <div className="relative z-10 space-y-5 h-full flex flex-col max-w-6xl">
        <PageHeader
          eyebrow="Replay"
          title="运行轨迹"
          description="把一次回答拆成 span 瀑布。核对耗时、token、失败点——不是看热闹。"
          actions={
            <div className="flex items-center gap-2">
              <button
                onClick={() => setFailuresOnly(!failuresOnly)}
                className={`chip ${failuresOnly ? "chip-accent" : ""}`}
              >
                {failuresOnly ? "仅失败" : "全部"}
              </button>
              <button
                onClick={fetchTraces}
                className="btn-accent px-3 py-1.5 rounded-lg text-[11px] flex items-center gap-1.5"
              >
                <RefreshCw className="w-3.5 h-3.5" />
                刷新
              </button>
            </div>
          }
        />

        {error && (
          <div className="flex items-center gap-2 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs">
            <AlertCircle className="w-4 h-4 shrink-0" />
            {error}
          </div>
        )}

        {loading && (
          <div className="flex items-center gap-2 text-xs text-[#6e6b63] dark:text-[#a19f96]">
            <Loader2 className="w-4 h-4 animate-spin" /> 正在加载...
          </div>
        )}

        {!loading && traces.length === 0 && !error && (
          <EmptyState
            icon={<BrandMark size={48} className="!rounded-[16px]" />}
            title="还没有可回放的轨迹"
            description={
              messageParam
                ? "这条消息的 trace 尚未入库，稍后刷新。"
                : "去对话一次。回答结束后，这里会出现一条可以拆开看的 span 瀑布。"
            }
          />
        )}

        {!loading && traces.length > 0 && (
          <div className="flex-1 grid grid-cols-[1fr_2fr] gap-5 min-h-0">
            {/* 左列:列表 */}
            <div className="card-surface rounded-2xl overflow-hidden flex flex-col">
              <div className="overflow-y-auto flex-1 divide-y divide-[#e6e2d8]/60 dark:divide-[#282724]/60">
                {displayed.map((t) => (
                  <button
                    key={t.traceId}
                    onClick={() => {
                      setSelectedTraceId(t.traceId);
                      setSearchParams({ trace: t.traceId }, { replace: true });
                    }}
                    className={`w-full p-3.5 text-left text-xs transition-colors hover:bg-[#f3f0e6]/50 dark:hover:bg-[#22211e] ${
                      selectedTraceId === t.traceId
                        ? "bg-[#eae6db] dark:bg-[#262522] border-l-[3px] border-[#da7756]"
                        : ""
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-1">
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
                    </div>
                    <div className="flex items-center gap-3 text-[#1f1e1d] dark:text-[#edece8] font-medium">
                      <span className="flex items-center gap-1">
                        <Timer className="w-3 h-3" /> {fmtMs(t.durationMs)}
                      </span>
                      <span className="text-[#6e6b63] dark:text-[#a19f96] font-normal">
                        {fmtInt(t.promptTokens + t.completionTokens)} tokens
                      </span>
                      <span className="text-[#6e6b63] dark:text-[#a19f96] font-normal">
                        {fmtCost(t.cost, t.currency)}
                      </span>
                      {t.failures > 0 && (
                        <span className="text-rose-500">{t.failures} 失败</span>
                      )}
                    </div>
                  </button>
                ))}
              </div>
              <button
                onClick={() => setLimit((l) => l + 20)}
                className="p-3 text-xs text-[#da7756] hover:bg-[#f3f0e6] dark:hover:bg-[#22211e] font-medium transition-colors"
              >
                加载更多
              </button>
            </div>

            {/* 右列:瀑布 + 详情 */}
            <div className="space-y-4 overflow-y-auto">
              {detailLoading && (
                <div className="flex items-center gap-2 text-xs text-[#6e6b63] dark:text-[#a19f96]">
                  <Loader2 className="w-4 h-4 animate-spin" /> 加载 trace
                  详情...
                </div>
              )}
              {!detailLoading && traceDetail && (
                <SpanWaterfall trace={traceDetail} onSelect={() => {}} />
              )}
              {!detailLoading && !traceDetail && selectedTraceId && (
                <div className="card-surface rounded-2xl p-8 text-center text-xs text-[#918d83]">
                  选中 trace 详情暂不可用
                </div>
              )}
              {!selectedTraceId && (
                <div className="card-surface rounded-2xl p-8 text-center text-xs text-[#918d83]">
                  从左侧列表选择一个 trace 查看瀑布
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
