import React, { useState } from "react";
import type {
  TraceDetail,
  TraceSpanNode,
} from "@/shared/types/api.types";
import { fmtMs, spanLabel } from "@/shared/lib/format";
import { SpanDetail } from "./SpanDetail";

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

export const SpanWaterfall: React.FC<{
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
