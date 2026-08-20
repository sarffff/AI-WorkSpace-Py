import React from "react";
import type { TraceSpanNode } from "@/shared/types/api.types";
import { fmtCost, fmtInt, fmtMs, spanLabel } from "@/shared/lib/format";
import { DetailRow } from "./DetailRow";

export const SpanDetail: React.FC<{ node: TraceSpanNode }> = ({ node }) => {
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
