import React from "react";
import { useNavigate } from "react-router-dom";

export const WORKSPACE_CAPABILITIES = [
  {
    key: "rag",
    label: "混合检索",
    hint: "dense + sparse · RRF",
    path: "/knowledge",
  },
  {
    key: "tools",
    label: "工具轨迹",
    hint: "跨回合回灌",
    path: "/chat",
  },
  {
    key: "cache",
    label: "语义缓存",
    hint: "按版本分桶",
    path: "/dashboard",
  },
  {
    key: "guard",
    label: "安全护栏",
    hint: "注入中和",
    path: "/chat",
  },
  {
    key: "trace",
    label: "运行回放",
    hint: "span 瀑布",
    path: "/traces",
  },
  {
    key: "prompt",
    label: "提示词实验",
    hint: "只读版本对比",
    path: "/prompts",
  },
] as const;

interface CapabilityStripProps {
  interactive?: boolean;
  compact?: boolean;
  className?: string;
}

export const CapabilityStrip: React.FC<CapabilityStripProps> = ({
  interactive = true,
  compact = false,
  className = "",
}) => {
  const navigate = useNavigate();

  return (
    <div className={`flex flex-wrap gap-2 ${className}`}>
      {WORKSPACE_CAPABILITIES.map((cap, i) => {
        const inner = (
          <>
            <span className="capability-dot" />
            <span className="font-medium">{cap.label}</span>
            {!compact && (
              <span className="text-[#918d83] hidden sm:inline">{cap.hint}</span>
            )}
          </>
        );
        const delay = { animationDelay: `${0.08 + i * 0.05}s` };
        if (!interactive) {
          return (
            <span
              key={cap.key}
              className="capability-pill anim-fade-up"
              style={delay}
            >
              {inner}
            </span>
          );
        }
        return (
          <button
            key={cap.key}
            type="button"
            onClick={() => navigate(cap.path)}
            className="capability-pill anim-fade-up"
            style={delay}
          >
            {inner}
          </button>
        );
      })}
    </div>
  );
};
