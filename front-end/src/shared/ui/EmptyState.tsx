import React from "react";

interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
  children?: React.ReactNode;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon,
  title,
  description,
  action,
  children,
}) => (
  <div className="empty-stage anim-fade-up">
    {icon && (
      <div className="relative mb-5">
        <div className="absolute inset-0 rounded-2xl bg-[#da7756] blur-2xl opacity-25" />
        <div className="relative">{icon}</div>
      </div>
    )}
    <h2 className="font-display text-[22px] font-semibold text-[#1f1e1d] dark:text-[#edece8]">
      {title}
    </h2>
    {description && (
      <p className="text-sm text-[#6e6b63] dark:text-[#a19f96] mt-2 leading-relaxed max-w-md">
        {description}
      </p>
    )}
    {action && <div className="mt-6">{action}</div>}
    {children}
  </div>
);
