import React, { useEffect, useRef, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useNavigate, useLocation } from "react-router-dom";
import { RootState } from "@/app/providers/store";
import { clearAuth } from "@/entities/auth/model/authSlice";
import { apiClient } from "@/shared/api/client";
import { useTheme } from "@/shared/lib/ThemeContext";
import { BrandMark } from "@/shared/ui/BrandMark";
import {
  LayoutDashboard,
  MessageSquare,
  ClipboardCheck,
  BookOpen,
  ScrollText,
  FlaskConical,
  Route as RouteIcon,
  BarChart3,
  Settings,
  Sun,
  Moon,
  LogOut,
} from "lucide-react";

/**
 * 图标轨：企业工作台的主导航。
 *
 * 换掉旧的 chat-centric 侧栏——旧侧栏把"新对话 + 会话历史"顶在最高层级，让一个
 * 审核生产工具长得像 chatbot。这里主模块平权：工作台 / 对话·审核 / 台账 / 知识库 /
 * 作业指导 / 提示词 / 轨迹 / 运营指标。会话列表下沉到「对话」模块的上下文面板，
 * 作业指导从设置里提上来，提示词与运营指标从"有页面却进不去"接回导航。
 */
const MODULES = [
  { id: "dashboard", label: "工作台", icon: LayoutDashboard, path: "/dashboard" },
  { id: "chat", label: "对话 · 审核", icon: MessageSquare, path: "/chat" },
  { id: "reviews", label: "审核台账", icon: ClipboardCheck, path: "/reviews", badge: true },
  { id: "knowledge", label: "知识库", icon: BookOpen, path: "/knowledge" },
  { id: "skills", label: "作业指导", icon: ScrollText, path: "/skills" },
  { id: "prompts", label: "提示词工作台", icon: FlaskConical, path: "/prompts" },
  { id: "traces", label: "运行轨迹", icon: RouteIcon, path: "/traces" },
  { id: "metrics", label: "运营指标", icon: BarChart3, path: "/metrics" },
] as const;

export const NavRail: React.FC = () => {
  const dispatch = useDispatch();
  const navigate = useNavigate();
  const location = useLocation();
  const { theme, toggleTheme } = useTheme();
  const { user } = useSelector((state: RootState) => state.auth);

  const [pending, setPending] = useState(0);
  const [workspace, setWorkspace] = useState<{
    name: string;
    role: string;
  } | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const active = location.pathname.split("/")[1] || "dashboard";

  // 工作区信息拉一次；待办数随导航刷新（在台账里处置完再切走就会更新）
  useEffect(() => {
    apiClient
      .getWorkspace()
      .then((w) => setWorkspace({ name: w.name, role: w.role }))
      .catch(() => {});
  }, []);

  useEffect(() => {
    apiClient
      .getReviews(true, 200)
      .then((r) => setPending(r.items.length))
      .catch(() => {});
  }, [location.pathname]);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const handleLogout = async () => {
    try {
      await apiClient.logout();
    } catch {
      /* 服务端登出失败也清本地 */
    }
    dispatch(clearAuth());
    navigate("/login");
  };

  const displayName = user?.name || user?.username || user?.email || "用户";
  const initial = displayName.charAt(0).toUpperCase();

  return (
    <aside className="w-[68px] shrink-0 h-full flex flex-col items-center py-3 bg-[#f3f0e6]/90 dark:bg-[#1a1917]/90 backdrop-blur-sm border-r border-[#e6e2d8] dark:border-[#282724] select-none relative z-20">
      <button
        onClick={() => navigate("/dashboard")}
        className="mb-3 transition-transform hover:scale-105"
        title="有据工作台"
        aria-label="有据工作台"
      >
        <BrandMark size={34} />
      </button>

      <nav className="flex-1 flex flex-col items-center gap-1.5 w-full">
        {MODULES.map((m) => {
          const on = active === m.id;
          const Icon = m.icon;
          return (
            <button
              key={m.id}
              onClick={() => navigate(m.path)}
              className={`group relative w-11 h-11 rounded-xl flex items-center justify-center transition-all duration-200 ${
                on
                  ? "bg-[#eae6db] dark:bg-[#262522] text-[#da7756]"
                  : "text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#eae6db]/60 dark:hover:bg-[#22211e]"
              }`}
              aria-label={m.label}
              aria-current={on ? "page" : undefined}
            >
              {on && (
                <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 rounded-full bg-[#da7756]" />
              )}
              <Icon className="w-[18px] h-[18px]" />
              {"badge" in m && m.badge && pending > 0 && (
                <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 rounded-full bg-[#da7756] text-white text-[9px] font-bold flex items-center justify-center">
                  {pending > 99 ? "99+" : pending}
                </span>
              )}
              <span className="pointer-events-none absolute left-[52px] px-2 py-1 rounded-md bg-[#1f1e1d] dark:bg-[#33312d] text-white text-[11px] font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity z-30 shadow-lg">
                {m.label}
              </span>
            </button>
          );
        })}
      </nav>

      <div className="flex flex-col items-center gap-1.5 w-full pt-2 mt-1 border-t border-[#e6e2d8]/70 dark:border-[#282724]/70">
        <button
          onClick={() => navigate("/settings")}
          className={`group relative w-11 h-11 rounded-xl flex items-center justify-center transition-all ${
            active === "settings"
              ? "bg-[#eae6db] dark:bg-[#262522] text-[#da7756]"
              : "text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#eae6db]/60 dark:hover:bg-[#22211e]"
          }`}
          aria-label="设置"
        >
          {active === "settings" && (
            <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 rounded-full bg-[#da7756]" />
          )}
          <Settings className="w-[18px] h-[18px]" />
          <span className="pointer-events-none absolute left-[52px] px-2 py-1 rounded-md bg-[#1f1e1d] dark:bg-[#33312d] text-white text-[11px] font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity z-30 shadow-lg">
            设置
          </span>
        </button>

        <button
          onClick={toggleTheme}
          className="group relative w-11 h-11 rounded-xl flex items-center justify-center text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#eae6db]/60 dark:hover:bg-[#22211e] transition-all"
          aria-label={theme === "dark" ? "切换到浅色" : "切换到深色"}
        >
          {theme === "dark" ? (
            <Sun className="w-[18px] h-[18px] text-amber-400" />
          ) : (
            <Moon className="w-[18px] h-[18px]" />
          )}
          <span className="pointer-events-none absolute left-[52px] px-2 py-1 rounded-md bg-[#1f1e1d] dark:bg-[#33312d] text-white text-[11px] font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity z-30 shadow-lg">
            {theme === "dark" ? "浅色" : "深色"}
          </span>
        </button>

        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="w-9 h-9 rounded-full bg-[#da7756] text-white text-xs font-bold flex items-center justify-center mt-0.5 shadow-sm hover:brightness-110 transition"
            aria-label="账户菜单"
            aria-expanded={menuOpen}
          >
            {initial}
          </button>
          {menuOpen && (
            <div className="absolute bottom-0 left-[52px] w-56 card-surface rounded-xl p-3 z-40 anim-fade-up">
              <div className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8] truncate">
                {displayName}
              </div>
              {user?.email && (
                <div className="text-[11px] text-[#918d83] truncate">
                  {user.email}
                </div>
              )}
              {workspace && (
                <div className="mt-2 pt-2 border-t border-[#e6e2d8] dark:border-[#282724] flex items-center justify-between gap-2">
                  <span className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] truncate">
                    {workspace.name}
                  </span>
                  <span className="chip text-[9px]">
                    {workspace.role === "admin" ? "管理员" : "成员"}
                  </span>
                </div>
              )}
              <button
                onClick={handleLogout}
                className="mt-2 w-full py-1.5 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-600 dark:text-rose-400 text-[11px] font-medium flex items-center justify-center gap-1.5 border border-rose-500/20"
              >
                <LogOut className="w-3 h-3" />
                退出登录
              </button>
            </div>
          )}
        </div>
      </div>

    </aside>
  );
};

