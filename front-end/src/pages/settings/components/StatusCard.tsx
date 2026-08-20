import React from "react";

export const StatusCard: React.FC<{
    icon: React.ReactNode;
    title: string;
    value: string;
    ok?: boolean;
}> = ({ icon, title, value, ok }) => (
    <div className="card-surface card-lift p-5 rounded-2xl space-y-1.5 relative z-10">
        <div
            className={`flex items-center gap-2 text-xs font-semibold tracking-wide ${
                ok
                    ? "text-emerald-600 dark:text-emerald-400"
                    : "text-[#6e6b63] dark:text-[#a19f96]"
            }`}
        >
            {icon}
            {title}
        </div>
        <div className="text-lg font-bold text-[#1f1e1d] dark:text-[#edece8]">
            {value}
        </div>
    </div>
);
