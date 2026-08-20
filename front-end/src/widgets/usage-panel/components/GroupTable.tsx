import React from "react";
import type { UsageGroup } from "@/shared/types/api.types";
import { fmtCost, fmtInt, fmtMs } from "@/shared/lib/format";

export const GroupTable: React.FC<{
  title: string;
  rows: UsageGroup[];
  labelOf: (row: UsageGroup) => string;
}> = ({ title, rows, labelOf }) => {
  if (!rows.length) return null;
  return (
    <div className="space-y-1.5">
      <h4 className="text-[11px] font-semibold text-[#6e6b63] dark:text-[#a19f96]">
        {title}
      </h4>
      <div className="space-y-0.5">
        {rows.map((row, index) => (
          <div
            key={`${labelOf(row)}-${row.currency ?? ""}-${index}`}
            className="flex items-center gap-3 px-3 py-1.5 rounded-lg bg-[#faf9f5] dark:bg-[#191817] text-[11px]"
          >
            <span className="flex-1 truncate text-[#1f1e1d] dark:text-[#edece8]">
              {labelOf(row)}
            </span>
            <span className="text-[#6e6b63] dark:text-[#a19f96]">{row.calls} 次</span>
            <span className="text-[#6e6b63] dark:text-[#a19f96]">
              {fmtInt(row.promptTokens + row.completionTokens)} tok
            </span>
            <span className="text-[#6e6b63] dark:text-[#a19f96]">
              均 {fmtMs(row.avgMs)}
            </span>
            <span className="w-20 text-right text-[#6e6b63] dark:text-[#a19f96]">
              {fmtCost(row.cost, row.currency)}
            </span>
            {row.failures > 0 && (
              <span className="text-red-500">{row.failures} 失败</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
};
