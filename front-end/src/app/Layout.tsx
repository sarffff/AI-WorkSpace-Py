import React from "react";
import { Outlet } from "react-router-dom";
import { NavRail } from "@/widgets/nav-rail/ui/NavRail";
import { ContextPanel } from "@/widgets/context-panel/ui/ContextPanel";
import { Header } from "@/widgets/header/ui/Header";

/**
 * 三区外壳：图标轨 | 上下文面板(按模块出现) | 顶栏 + 主内容。
 *
 * 换掉旧的"单侧栏(被会话历史占满) + 顶栏"——那是聊天软件的形状。企业审核工作台
 * 让主模块在图标轨上平权，会话历史下沉为「对话」模块的上下文，其余浏览型页面全宽。
 */
export const Layout: React.FC = () => {
  return (
    <div className="flex h-screen w-screen overflow-hidden app-atmosphere text-[#1f1e1d] dark:text-[#edece8] font-sans transition-colors duration-200">
      <NavRail />
      <ContextPanel />
      <div className="flex-1 flex flex-col h-full min-w-0 relative z-10">
        <Header />
        <main className="flex-1 overflow-hidden relative">
          <Outlet />
        </main>
      </div>
    </div>
  );
};
