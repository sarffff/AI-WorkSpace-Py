import React from "react";

export const StatCard: React.FC<{
    label: string;
    value: string;
    icon: React.ReactNode;
    accent?: boolean;
    ok?: boolean;
    /**
     * 数字下面的一行小字。加它是因为有些计数**不能只给一个数**：
     * admin 的分块总数里含成员个人文档的块，而那些不参与他的检索——
     * 光一个总数会让人以为全都能被引用到。
     */
    hint?: string;
}> = ({ label, value, icon, accent, ok, hint }) => (
    <div className="card-surface card-lift rounded-2xl p-5 space-y-2">
        <div className="flex items-center gap-1.5 text-[10px] font-semibold tracking-wide text-[#6e6b63] dark:text-[#a19f96]">
            <span className="text-[#da7756]">{icon}</span>
            {label}
        </div>
        <div
            className={`text-2xl font-bold ${
                ok
                    ? "text-emerald-600 dark:text-emerald-400"
                    : accent
                      ? "text-[#da7756]"
                      : "text-[#1f1e1d] dark:text-[#edece8]"
            }`}
            style={{ fontFamily: "var(--font-mono)" }}
        >
            {value}
        </div>
        {hint && (
            <div className="text-[10px] text-[#918d83] leading-snug">{hint}</div>
        )}
    </div>
);
