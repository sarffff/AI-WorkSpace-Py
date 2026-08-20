import React from "react";

export const ConfigRow: React.FC<{ label: string; value: string; badge?: "ok" }> = ({
    label,
    value,
    badge,
}) => (
    <div>
        <div className="text-[10px] font-semibold tracking-wide text-[#6e6b63] dark:text-[#a19f96] mb-1">
            {label}
        </div>
        <div className="flex items-center gap-2">
            <code className="text-xs text-[#1f1e1d] dark:text-[#edece8] font-mono break-all">
                {value}
            </code>
            {badge === "ok" && (
                <span className="px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 text-[10px] font-semibold">
                    已启用
                </span>
            )}
        </div>
    </div>
);
