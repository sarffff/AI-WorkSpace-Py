import React from "react";

export const StatCard: React.FC<{
    label: string;
    value: string;
    icon: React.ReactNode;
    accent?: boolean;
    ok?: boolean;
}> = ({ label, value, icon, accent, ok }) => (
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
    </div>
);
