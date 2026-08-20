import React from "react";

export const CapCell: React.FC<{
    icon: React.ReactNode;
    label: string;
    on: boolean;
}> = ({ icon, label, on }) => (
    <div
        className={`flex items-center gap-2.5 px-3.5 py-3 rounded-xl border ${
            on
                ? "border-[#da7756]/25 bg-[#da7756]/8"
                : "border-[#e3dfd5] dark:border-[#2e2d2a] bg-[#f3f0e6]/50 dark:bg-[#201f1c]/50"
        }`}
    >
        <span className={on ? "text-[#da7756]" : "text-[#918d83]"}>{icon}</span>
        <span className="text-xs font-medium text-[#1f1e1d] dark:text-[#edece8]">
            {label}
        </span>
        <span
            className={`ml-auto text-[10px] font-semibold ${
                on
                    ? "text-emerald-600 dark:text-emerald-400"
                    : "text-[#918d83]"
            }`}
        >
            {on ? "可用" : "未注册"}
        </span>
    </div>
);
