import React from "react";
import { agentLabel, fmtRate } from "./agentMeta";

/**
 * 子代理角色分布 + 失败率。
 *
 * 失败率按角色分开而不是给一个总数：researcher 常失败和 critic 常失败是两种
 * 完全不同的病。前者多半是主代理写的任务描述不够自包含（子代理看不到对话历史），
 * 后者多半是它没拿到可审的材料。总数会把这两件事平均掉。
 */
export const RoleBars: React.FC<{
  rows: {
    role: string | null;
    runs: number;
    failed: number;
    failureRate: number | null;
    avgRounds: number | null;
  }[];
}> = ({ rows }) => {
  const max = Math.max(...rows.map((row) => row.runs), 1);
  return (
    <div className="space-y-1.5">
      <h4 className="text-[11px] font-semibold text-[#6e6b63] dark:text-[#a19f96]">
        子代理调用分布
      </h4>
      <div className="space-y-1">
        {rows.map((row) => (
          <div
            key={row.role ?? "main"}
            className="flex items-center gap-2.5 text-[11px]"
          >
            <span className="w-20 shrink-0 truncate text-[#1f1e1d] dark:text-[#edece8]">
              {agentLabel(row.role)}
            </span>
            <div className="flex-1 h-2 rounded-full bg-[#faf9f5] dark:bg-[#191817] overflow-hidden">
              <div
                className="h-full rounded-full bg-violet-400/70"
                style={{ width: `${(row.runs / max) * 100}%` }}
              />
            </div>
            <span className="w-8 text-right text-[#6e6b63] dark:text-[#a19f96]">
              {row.runs}
            </span>
            <span
              className={`w-14 text-right ${
                row.failed > 0
                  ? "text-red-500"
                  : "text-[#6e6b63] dark:text-[#a19f96]"
              }`}
            >
              {row.failed > 0 ? `失败 ${fmtRate(row.failureRate)}` : "全部成功"}
            </span>
            <span className="w-14 text-right text-[#918d83]">
              均 {(row.avgRounds ?? 0).toFixed(1)} 轮
            </span>
          </div>
        ))}
      </div>
    </div>
  );
};
