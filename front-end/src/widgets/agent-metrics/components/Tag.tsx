import React from "react";

export const Tag: React.FC<{ icon: React.ReactNode; text: string }> = ({
  icon,
  text,
}) => (
  <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-[#faf9f5] dark:bg-[#191817] text-[#6e6b63] dark:text-[#a19f96]">
    {icon}
    {text}
  </span>
);
