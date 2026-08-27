import React from "react";

/**
 * 模式型能力的单元格。与 CapCell 同一骨架，但值不是「可用/未注册」而是
 * 具体档位（如审批的 off/write/listed）——这类能力没有开与关，只有策略不同。
 */
export const ModeCell: React.FC<{
    icon: React.ReactNode;
    label: string;
    /** 展示用的档位文案，如「写操作需确认」 */
    mode: string;
    active: boolean;
    title?: string;
}> = ({ icon, label, mode, active, title }) => (
    <div
        title={title}
        className={`flex items-center gap-2.5 px-3.5 py-3 rounded-xl border ${
            active
                ? "border-[#da7756]/25 bg-[#da7756]/8"
                : "border-[#e3dfd5] dark:border-[#2e2d2a] bg-[#f3f0e6]/50 dark:bg-[#201f1c]/50"
        }`}
    >
        <span className={active ? "text-[#da7756]" : "text-[#918d83]"}>
            {icon}
        </span>
        <span className="text-xs font-medium text-[#1f1e1d] dark:text-[#edece8]">
            {label}
        </span>
        <span
            className={`ml-auto text-[10px] font-semibold ${
                active
                    ? "text-[#da7756]"
                    : "text-[#918d83]"
            }`}
        >
            {mode}
        </span>
    </div>
);
