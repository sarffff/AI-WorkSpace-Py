import React from "react";
import { Outlet } from "react-router-dom";
import { NavRail } from "@/widgets/nav-rail/ui/NavRail";
import { Header } from "@/widgets/header/ui/Header";

/**
 * 两区外壳：图标轨 | 顶栏 + 主内容。
 *
 * 没有第三栏。上一版有一个"上下文面板"按路由切换内容（对话模块里是会话列表），
 * 而工单台不需要它：队列本身是列表页，工单详情的上下文（原文、轨迹、台账）
 * 都在那一页里跟着走。多一栏只会让"现在看的是哪张单"变成两个地方的事。
 */
export const Layout: React.FC = () => {
  return (
    <div className="flex h-screen w-screen overflow-hidden app-atmosphere text-ink font-sans">
      <NavRail />
      <div className="flex-1 flex flex-col h-full min-w-0 relative z-10">
        <Header />
        <main className="flex-1 overflow-hidden relative">
          <Outlet />
        </main>
      </div>
    </div>
  );
};
