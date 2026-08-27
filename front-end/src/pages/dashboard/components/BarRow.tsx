import React from "react";

export const BarRow: React.FC<{
  label: string;
  /** 悬停时展示完整内容——label 常被 w-28 truncate 截断 */
  title?: string;
  value: string;
  pct: number;
  delay?: number;
}> = ({ label, title, value, pct, delay = 0 }) => (
  <div className="flex items-center gap-3 text-[11px]">
    <span
      className="w-28 shrink-0 truncate text-[#1f1e1d] dark:text-[#edece8]"
      title={title ?? label}
    >
      {label}
    </span>
    <div className="flex-1 h-2 rounded-full bg-[#f3f0e6] dark:bg-[#262522] overflow-hidden">
      <div
        className="h-full rounded-full bg-gradient-to-r from-[#da7756] to-[#e0845f] anim-bar"
        style={{
          width: `${Math.min(pct * 100, 100)}%`,
          animationDelay: `${delay}s`,
        }}
      />
    </div>
    <span className="w-28 text-right text-[#6e6b63] dark:text-[#a19f96] truncate">
      {value}
    </span>
  </div>
);
