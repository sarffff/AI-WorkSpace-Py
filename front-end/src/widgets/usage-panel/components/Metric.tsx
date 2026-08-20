import React from "react";

export const Metric: React.FC<{ label: string; value: string; warn?: boolean }> = ({
  label,
  value,
  warn,
}) => (
  <div className="rounded-xl bg-[#faf9f5] dark:bg-[#191817] px-3 py-2.5">
    <div className="text-[10px] text-[#918d83] mb-0.5">{label}</div>
    <div
      className={`text-sm font-semibold ${
        warn ? "text-red-500" : "text-[#1f1e1d] dark:text-[#edece8]"
      }`}
    >
      {value}
    </div>
  </div>
);
