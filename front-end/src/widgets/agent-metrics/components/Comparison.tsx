import React from "react";
import type { DelegationComparison } from "@/shared/types/api.types";
import { fmtCost, fmtInt, fmtMs } from "@/shared/lib/format";

/**
 * 委派 vs 未委派：轮次、成本、延迟。
 *
 * 这是整个面板真正要看的东西。"委派率 27%" 本身说明不了任何问题——委派值不值
 * 取决于它多花了几倍的钱、慢了几倍，而这个比值只有把两组放在一起才看得出来。
 *
 * 倍数用未委派那组当基准。不给百分比而给"×2.4"：成本从 ¥0.011 涨到 ¥0.043
 * 说成"增长 291%"要在脑子里换算一次，说成"贵 3.9 倍"是可以直接判断的。
 */
export const Comparison: React.FC<{ rows: DelegationComparison[] }> = ({
  rows,
}) => {
  if (!rows.length) {
    return (
      <div className="rounded-xl bg-[#faf9f5] dark:bg-[#191817] px-3 py-2.5 text-[10px] text-[#6e6b63] dark:text-[#a19f96]">
        成本与延迟对比需要开启埋点（<code>TELEMETRY_ENABLED=true</code>）——
        这两个数来自 trace_spans，不在执行记录里重复存。
      </div>
    );
  }

  // 同一分桶可能有多行（不同币种各一行）。取记录数最多的那个币种作代表：
  // 把 CNY 和 USD 相加是错的，而并排显示两套倍数只会让这张表读不下去。
  const pick = (delegated: boolean) => {
    const candidates = rows.filter((row) => row.delegated === delegated);
    return candidates.sort((a, b) => b.runs - a.runs)[0] ?? null;
  };
  const yes = pick(true);
  const no = pick(false);

  const ratio = (
    a: number | null | undefined,
    b: number | null | undefined,
  ) => {
    if (!a || !b) return null;
    return a / b;
  };

  const lines: {
    label: string;
    yes: string;
    no: string;
    times: number | null;
  }[] = [
    {
      label: "回答数",
      yes: yes ? fmtInt(yes.runs) : "—",
      no: no ? fmtInt(no.runs) : "—",
      times: null,
    },
    {
      label: "平均轮次",
      yes: yes?.avgRounds != null ? yes.avgRounds.toFixed(1) : "—",
      no: no?.avgRounds != null ? no.avgRounds.toFixed(1) : "—",
      times: ratio(yes?.avgRounds, no?.avgRounds),
    },
    {
      label: "平均成本",
      yes: yes ? fmtCost(yes.avgCost, yes.currency) : "—",
      no: no ? fmtCost(no.avgCost, no.currency) : "—",
      times: ratio(yes?.avgCost, no?.avgCost),
    },
    {
      label: "平均耗时",
      yes: fmtMs(yes?.avgTurnMs ?? null),
      no: fmtMs(no?.avgTurnMs ?? null),
      times: ratio(yes?.avgTurnMs, no?.avgTurnMs),
    },
  ];

  return (
    <div className="space-y-1.5">
      <h4 className="text-[11px] font-semibold text-[#6e6b63] dark:text-[#a19f96]">
        委派的代价
      </h4>
      <div className="rounded-xl bg-[#faf9f5] dark:bg-[#191817] px-3 py-2 space-y-1">
        <div className="flex items-center gap-2 text-[10px] text-[#918d83] pb-1 border-b border-[#e8e5da] dark:border-[#2a2825]">
          <span className="flex-1" />
          <span className="w-16 text-right">委派</span>
          <span className="w-16 text-right">未委派</span>
          <span className="w-14 text-right">倍数</span>
        </div>
        {lines.map((line) => (
          <div
            key={line.label}
            className="flex items-center gap-2 text-[11px] py-0.5"
          >
            <span className="flex-1 text-[#6e6b63] dark:text-[#a19f96]">
              {line.label}
            </span>
            <span className="w-16 text-right font-medium text-[#1f1e1d] dark:text-[#edece8]">
              {line.yes}
            </span>
            <span className="w-16 text-right text-[#6e6b63] dark:text-[#a19f96]">
              {line.no}
            </span>
            <span
              className={`w-14 text-right ${
                line.times && line.times > 2
                  ? "text-amber-500"
                  : "text-[#918d83]"
              }`}
            >
              {line.times ? `×${line.times.toFixed(1)}` : "—"}
            </span>
          </div>
        ))}
      </div>
      {yes && no && yes.runs < 5 && (
        // 样本太少时必须说出来。这张表的视觉说服力远超它此刻应有的可信度——
        // 3 次委派算出来的"贵 3.9 倍"和 300 次算出来的是完全不同的证据。
        <div className="text-[10px] text-amber-500">
          委派样本只有 {yes.runs} 次，倍数仅供参考，还不足以下结论。
        </div>
      )}
    </div>
  );
};
