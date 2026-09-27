import React, { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { apiClient } from "@/shared/api/client";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { ChatListPanel } from "./ChatListPanel";

/**
 * 上下文面板：按当前模块渲染它的常驻列表/筛选，可折叠。
 *
 * 只在"常驻列表值得占一列"的模块出现（对话的会话列表、台账的待办筛选），其余模块
 * 返回 null，主内容占满。折叠状态存 localStorage、跨模块与刷新保持——收起后仍留一条
 * 窄边带展开按钮，别让人找不到怎么再打开。
 */

const COLLAPSE_KEY = "ctxPanel:collapsed";

const REVIEW_FILTERS = [
  { id: "pending", label: "待办" },
  { id: "all", label: "全部" },
] as const;

const ReviewContextPanel: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const [pending, setPending] = useState(0);
  const filter = new URLSearchParams(location.search).get("filter") || "pending";

  useEffect(() => {
    apiClient
      .getReviews(true, 200)
      .then((r) => setPending(r.items.length))
      .catch(() => {});
  }, [location.key]);

  return (
    <div className="p-4 space-y-1">
      <div className="label-eyebrow mb-2">审核台账</div>
      {REVIEW_FILTERS.map((f) => {
        const on = filter === f.id;
        return (
          <button
            key={f.id}
            onClick={() => navigate(`/reviews?filter=${f.id}`)}
            className={`w-full flex items-center justify-between px-3 py-2 rounded-xl text-xs transition-all ${
              on
                ? "bg-[#eae6db] dark:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] font-medium shadow-sm"
                : "text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#eae6db]/60 dark:hover:bg-[#22211e]"
            }`}
          >
            <span>{f.label}</span>
            {f.id === "pending" && pending > 0 && (
              <span className="chip chip-accent text-[9px] px-1.5 py-0">
                {pending}
              </span>
            )}
          </button>
        );
      })}
      <p className="px-2 pt-3 text-[11px] text-[#918d83] leading-relaxed">
        待办 = 需要人判断、且还没人处置过的结论。别让它们堆进没人看的队列。
      </p>
    </div>
  );
};

export const ContextPanel: React.FC = () => {
  const location = useLocation();
  const mod = location.pathname.split("/")[1] || "dashboard";
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSE_KEY) === "1",
  );

  useEffect(() => {
    localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
  }, [collapsed]);

  let content: React.ReactNode = null;
  if (mod === "chat") content = <ChatListPanel />;
  else if (mod === "reviews") content = <ReviewContextPanel />;

  // 该模块本就没有上下文面板：主内容全宽，折叠状态无从谈起
  if (!content) return null;

  // 收起：留一条窄边带，顶上放展开按钮（悬停出 tooltip），别让入口消失
  if (collapsed) {
    return (
      <div className="w-9 shrink-0 h-full flex flex-col items-center pt-2.5 bg-[#f6f3ec]/80 dark:bg-[#181715]/80 backdrop-blur-sm border-r border-[#e6e2d8] dark:border-[#282724] relative z-10">
        <button
          onClick={() => setCollapsed(false)}
          className="group relative w-7 h-7 rounded-lg flex items-center justify-center text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#eae6db]/60 dark:hover:bg-[#22211e] transition-all"
          aria-label="展开侧栏"
        >
          <PanelLeftOpen className="w-4 h-4" />
          <span className="pointer-events-none absolute left-[34px] px-2 py-1 rounded-md bg-[#1f1e1d] dark:bg-[#33312d] text-white text-[11px] font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity z-30 shadow-lg">
            展开侧栏
          </span>
        </button>
      </div>
    );
  }

  return (
    <aside className="w-[264px] shrink-0 h-full overflow-hidden bg-[#f6f3ec]/80 dark:bg-[#181715]/80 backdrop-blur-sm border-r border-[#e6e2d8] dark:border-[#282724] relative z-10 flex flex-col">
      <div className="flex items-center justify-end px-2 pt-2 shrink-0">
        <button
          onClick={() => setCollapsed(true)}
          className="p-1.5 rounded-lg text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#eae6db]/60 dark:hover:bg-[#22211e] transition-colors"
          aria-label="收起侧栏"
          title="收起侧栏"
        >
          <PanelLeftClose className="w-4 h-4" />
        </button>
      </div>
      <div className="flex-1 overflow-hidden">{content}</div>
    </aside>
  );
};
