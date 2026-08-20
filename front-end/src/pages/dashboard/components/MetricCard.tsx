import React from "react";

export const MetricCard: React.FC<{
  label: string;
  value: string;
  icon: React.ReactNode;
  warn?: boolean;
  hint?: string;
}> = ({ label, value, icon, warn, hint }) => (
  <div className="card-surface card-lift rounded-2xl p-4 space-y-2">
    <div className="flex items-center gap-1.5 text-[10px] font-semibold tracking-wide text-[#6e6b63] dark:text-[#a19f96]">
      <span className={warn ? "text-rose-500" : "text-[#da7756]"}>{icon}</span>
      {label}
    </div>
    <div
      className={`text-xl font-bold leading-none ${
        warn ? "text-rose-500" : "text-[#1f1e1d] dark:text-[#edece8]"
      }`}
      style={{ fontFamily: "var(--font-mono)" }}
    >
      {value}
    </div>
    {hint && <div className="text-[10px] text-[#918d83]">{hint}</div>}
  </div>
);
