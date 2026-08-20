import React from "react";

export const DetailRow: React.FC<{
  label: string;
  value: React.ReactNode;
}> = ({ label, value }) => (
  <div className="flex items-center gap-2">
    <span className="text-[#918d83] shrink-0">{label}</span>
    <span className="text-[#1f1e1d] dark:text-[#edece8] font-medium truncate">
      {value}
    </span>
  </div>
);
