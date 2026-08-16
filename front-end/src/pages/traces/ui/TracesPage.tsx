import React, { useEffect, useState, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { apiClient } from "@/shared/api/client";
import type {
  TraceSummary,
  TraceDetail,
  TraceSpanNode,
} from "@/shared/types/api.types";
import { fmtCost, fmtInt, fmtMs, spanLabel } from "@/shared/lib/format";
import { PageHeader } from "@/shared/ui/PageHeader";
import { EmptyState } from "@/shared/ui/EmptyState";
import { BrandMark } from "@/shared/ui/BrandMark";
import {
  Loader2,
  RefreshCw,
  Timer,
  AlertCircle,
} from "lucide-react";

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

/* ---------- SpanWaterfall ---------- */
interface FlatSpan {
  node: TraceSpanNode;
  depth: number;
}

const flattenTree = (roots: TraceSpanNode[], depth = 0): FlatSpan[] => {
  const result: FlatSpan[] = [];
  for (const node of roots) {
    result.push({ node, depth });
    if (node.children) {
      result.push(...flattenTree(node.children, depth + 1));
    }
  }
  return result;
};

const SpanWaterfall: React.FC<{
  trace: TraceDetail;
  onSelect: (node: TraceSpanNode) => void;
}> = ({ trace, onSelect }) => {
  const rootDuration =
    trace.roots.reduce((max, r) => Math.max(max, r.durationMs ?? 0), 0) || 1;
  const rootStart = trace.roots.reduce(
    (min, r) =>
      Math.min(
        min,
        r.startedAt ? new Date(r.startedAt).getTime() : Number.MAX_SAFE_INTEGER,
      ),
    Number.MAX_SAFE_INTEGER,
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const flat = flattenTree(trace.roots);

  const handleSelect = (node: TraceSpanNode) => {
    setSelectedId(node.id);
    onSelect(node);
  };

  const selectedNode = flat.find((f) => f.node.id === selectedId)?.node;

  return (
    <div className="card-surface rounded-2xl space-y-3 p-5">
      <h3 className="label-eyebrow">Trace · {trace.traceId.slice(0, 8)}</h3>

      {/* 时间刻度 */}
      <div className="flex text-[10px] text-[#918d83] font-mono">
        <span className="flex-1">0</span>
        <span className="flex-1 text-center">
          {fmtMs(Math.round(rootDuration * 0.25))}
        </span>
        <span className="flex-1 text-center">
          {fmtMs(Math.round(rootDuration * 0.5))}
        </span>
        <span className="flex-1 text-center">
          {fmtMs(Math.round(rootDuration * 0.75))}
        </span>
        <span className="text-right">{fmtMs(rootDuration)}</span>
      </div>

      {/* 瀑布行 */}
      <div className="space-y-0.5">
        {flat.map(({ node, depth }) => {
          const duration = Math.max(node.durationMs ?? 0, 1);
          const leftPct = node.startedAt
            ? Math.max(
                ((new Date(node.startedAt).getTime() - rootStart) /
                  rootDuration) *
                  100,
                0,
              )
            : 0;
          const widthPct = Math.min(
            (duration / rootDuration) * 100,
            100 - leftPct,
          );
          const isSelected = node.id === selectedId;

          const statusColor =
            node.status === "ok"
              ? "bg-[#da7756]"
              : node.status === "cancelled"
                ? "bg-[#918d83]"
                : "bg-rose-500";

          return (
            <button
              key={node.id}
              onClick={() => handleSelect(node)}
              className={`w-full flex items-center gap-2 px-1.5 py-1 rounded text-[11px] text-left transition-colors ${
                isSelected
                  ? "bg-[#eae6db] dark:bg-[#262522]"
                  : "hover:bg-[#f3f0e6]/50 dark:hover:bg-[#22211e]"
              }`}
            >
              <span
                className="w-1.5 h-1.5 rounded-full shrink-0"
                style={{
                  background:
                    statusColor === "bg-[#da7756]"
                      ? "#da7756"
                      : statusColor === "bg-[#918d83]"
                        ? "#918d83"
                        : "#f43f5e",
                }}
              />
              <span
                className="shrink-0 text-[#918d83]"
                style={{ width: `${depth * 12}px` }}
              />
              <span className="w-28 shrink-0 truncate text-[#1f1e1d] dark:text-[#edece8]">
                {spanLabel(node.name)}
              </span>
              <div className="flex-1 h-3 bg-[#f3f0e6] dark:bg-[#262522] rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full ${statusColor} opacity-70`}
                  style={{
                    marginLeft: `${leftPct}%`,
                    width: `${Math.max(widthPct, 2)}%`,
                  }}
                />
              </div>
              <span className="w-14 text-right text-[#918d83]">
                {fmtMs(node.durationMs)}
              </span>
            </button>
          );
        })}
      </div>

      {/* 选中 span 详情 */}
      {selectedNode && <SpanDetail node={selectedNode} />}
    </div>
  );
};

/* ---------- SpanDetail ---------- */
const SpanDetail: React.FC<{ node: TraceSpanNode }> = ({ node }) => {
  let attributesStr = "{}";
  if (node.attributes) {
    try {
      attributesStr =
        typeof node.attributes === "string"
          ? node.attributes
          : JSON.stringify(node.attributes, null, 2);
    } catch {
      attributesStr = String(node.attributes);
    }
  }

  return (
    <div className="rounded-xl border border-[#e3dfd5] dark:border-[#2e2d2a] bg-[#faf9f5] dark:bg-[#191817] p-3.5 space-y-2.5">
      <h4 className="text-[11px] font-semibold text-[#1f1e1d] dark:text-[#edece8]">
        span 详情
      </h4>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
        <DetailRow label="名称" value={spanLabel(node.name)} />
        <DetailRow label="类型" value={node.kind} />
        <DetailRow
          label="状态"
          value={
            <span
              className={
                node.status === "ok"
                  ? "text-emerald-600 dark:text-emerald-400"
                  : node.status === "cancelled"
                    ? "text-[#918d83]"
                    : "text-rose-500"
              }
            >
              {node.status}
              {node.errorType && ` · ${node.errorType}`}
            </span>
          }
        />
        <DetailRow label="模型" value={node.model ?? "-"} />
        <DetailRow label="耗时" value={fmtMs(node.durationMs)} />
        <DetailRow
          label="Token"
          value={`${fmtInt(node.promptTokens ?? 0)} in / ${fmtInt(node.completionTokens ?? 0)} out`}
        />
        <DetailRow label="成本" value={fmtCost(node.cost, node.currency)} />
        <DetailRow
          label="Token 来源"
          value={node.tokenSource === "estimated" ? "估算" : "实际"}
        />
      </div>
      {attributesStr !== "{}" && (
        <div>
          <div className="text-[10px] font-semibold text-[#6e6b63] dark:text-[#a19f96] mb-1">
            属性
          </div>
          <pre
            className="text-[10px] text-[#1f1e1d] dark:text-[#edece8] bg-[#f3f0e6] dark:bg-[#201f1c] rounded-lg p-2.5 overflow-x-auto"
            style={{ fontFamily: "var(--font-mono)" }}
          >
            {attributesStr}
          </pre>
        </div>
      )}
    </div>
  );
};

const DetailRow: React.FC<{
  label: string;
  value: React.ReactNode;
}> = ({ label, value }) => (
  <div className="flex items-center gap-2">
    <span className="text-[#918d83] shrink-0">{label}</span>
    <span className="text-[#1f1e1d] dark:text-[#edece8] font-medium truncate">
      {value}
    </span>
  </div>
);
