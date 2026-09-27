import React from "react";
import type { TraceSpanNode } from "@/shared/types/api.types";
import { fmtCost, fmtInt, fmtMs, spanLabel } from "@/shared/lib/format";
import { DetailRow } from "./DetailRow";
import { AgentSignals, partitionAttributes } from "./AgentSignals";

export const SpanDetail: React.FC<{ node: TraceSpanNode }> = ({ node }) => {
  // attributes 可能是 JSON 字符串（后端 _encode_attributes 的产物）、已解析的对象，
  // 或者根本解析不了。三种都要落到"看得见"，不能因为解析失败就整块消失。
  let attributesStr = "{}";
  let signals: ReturnType<typeof partitionAttributes>["signals"] = [];
  if (node.attributes) {
    let parsed: Record<string, unknown> | null = null;
    try {
      parsed =
        typeof node.attributes === "string"
          ? (JSON.parse(node.attributes) as Record<string, unknown>)
          : (node.attributes as Record<string, unknown>);
    } catch {
      parsed = null;
    }

    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const split = partitionAttributes(parsed);
      signals = split.signals;
      // 只把**没被抬成命名行**的键留在原始块里。认识的那些已经在上面显示了,
      // 再原样重复一遍只是噪音。
      attributesStr =
        Object.keys(split.rest).length > 0
          ? JSON.stringify(split.rest, null, 2)
          : "{}";
    } else {
      // 解析不出来就原样打印,总比"属性"整块不见了好。
      attributesStr =
        typeof node.attributes === "string"
          ? node.attributes
          : String(node.attributes);
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
        {/* 模型/token/成本只有 llm、embedding 这类真调了外部模型的 span 才有。
            工具与检索是本地执行，硬摆一行"模型 -"只会让人以为数据丢了 */}
        {node.model && <DetailRow label="模型" value={node.model} />}
        <DetailRow label="耗时" value={fmtMs(node.durationMs)} />
        {(node.promptTokens != null || node.completionTokens != null) && (
          <DetailRow
            label="Token"
            value={`${fmtInt(node.promptTokens ?? 0)} in / ${fmtInt(node.completionTokens ?? 0)} out`}
          />
        )}
        {node.cost != null && (
          <DetailRow label="成本" value={fmtCost(node.cost, node.currency)} />
        )}
        {node.tokenSource && (
          <DetailRow
            label="Token 来源"
            value={node.tokenSource === "estimated" ? "估算" : "实际"}
          />
        )}
      </div>
      <AgentSignals signals={signals} />
      {attributesStr !== "{}" && (
        <div>
          <div className="text-[10px] font-semibold text-[#6e6b63] dark:text-[#a19f96] mb-1">
            {signals.length > 0 ? "其它属性" : "属性"}
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
