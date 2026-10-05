import React from "react";

interface PageHeaderProps {
  eyebrow?: string;
  /**
   * 页面标题。**顶层模块的页面不要传**：模块名已经在顶栏出现过一次，
   * 同屏两遍"工单队列"只会让界面显得没想过这件事。那种页面只传 description + actions，
   * 顶栏那个 h1 就是整页唯一的标题。
   */
  title?: string;
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
      {title && (
        <h2 className="font-display text-[26px] font-semibold text-ink tracking-tight">
          {title}
        </h2>
      )}
      {description && (
        <p
          className={[
            "text-sm text-ink-soft leading-relaxed max-w-xl",
            title ? "mt-1.5" : "mt-0",
          ].join(" ")}
        >
          {description}
        </p>
      )}
    </div>
    {actions && <div className="shrink-0 flex items-center gap-2">{actions}</div>}
  </div>
);
