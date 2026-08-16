import React from "react";

/**
 * 工作台徽标：三根不等宽的 span 横条，像缩小的轨迹瀑布。
 * 这是本项目最独特的视觉——不是对话气泡，是「看得见模型怎么跑」。
 */
export const BrandMark: React.FC<{
  size?: number;
  className?: string;
}> = ({ size = 36, className = "" }) => (
  <span
    className={`relative inline-flex items-center justify-center rounded-[11px] btn-accent shrink-0 ${className}`}
    style={{ width: size, height: size }}
    aria-hidden
  >
    <svg
      viewBox="0 0 24 24"
      width={Math.round(size * 0.52)}
      height={Math.round(size * 0.52)}
      fill="none"
    >
      <rect x="3" y="5" width="14" height="3.2" rx="1.6" fill="white" opacity="0.95" />
      <rect x="3" y="10.4" width="18" height="3.2" rx="1.6" fill="white" opacity="0.78" />
      <rect x="3" y="15.8" width="9" height="3.2" rx="1.6" fill="white" opacity="0.58" />
    </svg>
  </span>
);
