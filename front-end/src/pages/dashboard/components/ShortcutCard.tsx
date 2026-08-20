import React from "react";

export const ShortcutCard: React.FC<{
  icon: React.ReactNode;
  title: string;
  body: string;
  onClick: () => void;
}> = ({ icon, title, body, onClick }) => (
  <button
    onClick={onClick}
    className="card-surface card-lift rounded-2xl p-4 text-left space-y-2"
  >
    <div className="w-8 h-8 rounded-xl bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
      {icon}
    </div>
    <div className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
      {title}
    </div>
    <p className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] leading-relaxed">
      {body}
    </p>
  </button>
);
