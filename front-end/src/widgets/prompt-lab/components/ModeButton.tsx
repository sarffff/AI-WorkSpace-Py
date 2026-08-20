import React from "react";

export const ModeButton: React.FC<{
    active: boolean;
    onClick: () => void;
    icon: React.ReactNode;
    label: string;
}> = ({ active, onClick, icon, label }) => (
    <button
        onClick={onClick}
        className={`flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-medium transition-colors ${
            active
                ? "bg-white dark:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] shadow-sm"
                : "text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
        }`}
    >
        {icon}
        {label}
    </button>
);
