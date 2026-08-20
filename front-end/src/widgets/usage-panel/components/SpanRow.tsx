import React from "react";
import type { TraceSpanNode } from "@/shared/types/api.types";
import { fmtInt, fmtMs, spanLabel } from "@/shared/lib/format";

export const SpanRow: React.FC<{ node: TraceSpanNode; depth: number }> = ({
  node,
  depth,
}) => (
  <>
    <div
      className="flex items-center gap-2 text-[11px] py-0.5"
      style={{ paddingLeft: depth * 14 }}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full shrink-0 ${
          node.status === "ok"
            ? "bg-[#da7756]"
            : node.status === "cancelled"
              ? "bg-[#918d83]"
              : "bg-red-500"
        }`}
      />
      <span className="text-[#1f1e1d] dark:text-[#edece8]">{spanLabel(node.name)}</span>
      <span className="text-[#918d83]">{fmtMs(node.durationMs)}</span>
      {node.promptTokens !== null && (
        <span className="text-[#918d83]">
          {fmtInt((node.promptTokens ?? 0) + (node.completionTokens ?? 0))} tok
          {node.tokenSource === "estimated" && "（估算）"}
        </span>
      )}
      {node.errorType && <span className="text-red-500">{node.errorType}</span>}
    </div>
    {node.children.map((child) => (
      <SpanRow key={child.id} node={child} depth={depth + 1} />
    ))}
  </>
);
