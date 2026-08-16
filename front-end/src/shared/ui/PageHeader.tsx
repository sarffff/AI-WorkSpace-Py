import React from "react";

interface PageHeaderProps {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: React.ReactNode;
}

export const PageHeader: React.FC<PageHeaderProps> = ({
  eyebrow,
  title,
  description,
  actions,
}) => (
  <div className="flex items-start justify-between gap-4 relative z-10 anim-fade-up">
    <div className="min-w-0">
      {eyebrow && <div className="label-eyebrow mb-1.5">{eyebrow}</div>}
      <h1 className="font-display text-[26px] font-semibold text-[#1f1e1d] dark:text-[#edece8] tracking-tight">
        {title}
      </h1>
      {description && (
        <p className="text-xs text-[#6e6b63] dark:text-[#a19f96] mt-1.5 leading-relaxed max-w-xl">
          {description}
        </p>
      )}
    </div>
    {actions && <div className="shrink-0 flex items-center gap-2">{actions}</div>}
  </div>
);
