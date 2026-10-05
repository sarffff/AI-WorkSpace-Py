import React, { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { PauseOctagon } from "lucide-react";
import { apiClient } from "@/shared/api/client";

/**
 * 模块标题。`eyebrow` 是给快速扫视用的英文短语（轨道图标 + 英文小字
 * 比纯中文更快定位），正文一律中文。
 */
const TITLES: Record<string, { title: string; eyebrow: string }> = {
  queue: { title: "工单队列", eyebrow: "TICKET QUEUE" },
  tickets: { title: "工单详情", eyebrow: "TICKET" },
  approvals: { title: "审批收件箱", eyebrow: "AWAITING YOU" },
  governance: { title: "治理台", eyebrow: "GUARDRAILS" },
  metrics: { title: "指标看板", eyebrow: "METRICS" },
  knowledge: { title: "知识库", eyebrow: "KNOWLEDGE" },
  skills: { title: "作业指导", eyebrow: "SOP LIBRARY" },
  notifications: { title: "通知", eyebrow: "INBOX" },
  audit: { title: "审计", eyebrow: "AUDIT TRAIL" },
  workspace: { title: "工作区", eyebrow: "WORKSPACE" },
};

export const Header: React.FC = () => {
  const location = useLocation();
  const module = location.pathname.split("/")[1] || "queue";
  const meta = TITLES[module] ?? TITLES.queue;

  /**
   * 全局暂停状态在每个页面都要看得见。
   *
   * 它是唯一一个"看不见就会出事"的状态：暂停之后 Agent 不再执行任何写操作，
   * 坐席如果没注意到横幅，会对"为什么这单一直没动"做出错误的判断，
   * 然后去做本该由系统做的那一步。所以它进顶栏，而不只进治理台。
   */
  const [paused, setPaused] = useState<{ paused: boolean; reason: string | null }>({
    paused: false,
    reason: null,
  });

  useEffect(() => {
    let alive = true;
    apiClient
      .governorState()
      .then((state) =>
        alive && setPaused({ paused: state.paused, reason: state.pauseReason })
      )
      // 拿不到就当没暂停：这一条是提示，不是权限判据（真正的拦在后端执行那一步）
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [location.pathname]);

  return (
    <header className="h-14 shrink-0 flex items-center justify-between gap-4 px-6 border-b border-line bg-crust relative z-10">
      <div className="min-w-0">
        <div className="label-eyebrow">{meta.eyebrow}</div>
        <h1 className="font-display text-[17px] font-semibold text-ink leading-tight truncate">
          {meta.title}
        </h1>
      </div>

      {paused.paused && (
        <div
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-[12px] font-semibold text-state-bad"
          style={{
            background: "var(--c-bad-faint)",
            border: "1px solid color-mix(in srgb, var(--c-bad) 40%, transparent)",
          }}
          title={paused.reason ?? "未注明原因"}
          role="status"
        >
          <PauseOctagon className="w-4 h-4 shrink-0" />
          <span className="truncate max-w-[42ch]">
            全线暂停中{paused.reason ? ` · ${paused.reason}` : ""}
          </span>
        </div>
      )}
    </header>
  );
};
