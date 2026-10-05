import React, { useEffect, useRef, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useLocation, useNavigate } from "react-router-dom";
import {
  BarChart3,
  Bell,
  BookOpen,
  Fingerprint,
  Inbox,
  LogOut,
  Monitor,
  Moon,
  ScrollText,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sun,
  Users,
  type LucideIcon,
} from "lucide-react";
import { RootState } from "@/app/providers/store";
import { clearAuth } from "@/entities/auth/model/authSlice";
import { apiClient } from "@/shared/api/client";
import { NEXT_MODE, THEME_LABELS, useTheme, type ThemeMode } from "@/shared/lib/ThemeContext";
import { BrandMark } from "@/shared/ui/BrandMark";
import { NotificationBell } from "@/features/notifications/ui/NotificationBell";

interface Module {
  id: string;
  label: string;
  icon: LucideIcon;
  path: string;
  /** 这一格要不要挂一个"有人在等"的徽标 */
  alarm?: "approvals";
}

/**
 * 图标轨：工单台的模块集合，对着 Development_Process.md 的五层来。
 *
 * 顺序就是坐席的一天：先看队列（谁的事没办）→ 审批（等我点的）→ 治理台
 * （额度与暂停，出事时才进）→ 指标（这周办得怎么样）→ 资料（知识库 / SOP）→
 * 通知 / 审计 / 成员。
 *
 * 刻意没有"对话"这一格：Agent 办单的现场是那张工单的轨迹，不是一个聊天框。
 */
const MODULES: Module[] = [
  { id: "queue", label: "工单队列", icon: Inbox, path: "/queue" },
  { id: "approvals", label: "审批收件箱", icon: ShieldCheck, path: "/approvals", alarm: "approvals" },
  { id: "governance", label: "治理台", icon: SlidersHorizontal, path: "/governance" },
  { id: "metrics", label: "指标看板", icon: BarChart3, path: "/metrics" },
  { id: "knowledge", label: "知识库", icon: BookOpen, path: "/knowledge" },
  { id: "skills", label: "作业指导", icon: ScrollText, path: "/skills" },
  { id: "notifications", label: "通知", icon: Bell, path: "/notifications" },
  { id: "audit", label: "审计", icon: Fingerprint, path: "/audit" },
  { id: "workspace", label: "工作区", icon: Users, path: "/workspace" },
];

export const NavRail: React.FC = () => {
  const dispatch = useDispatch();
  const navigate = useNavigate();
  const location = useLocation();
  const { mode, theme, cycleMode } = useTheme();
  const { user } = useSelector((state: RootState) => state.auth);

  const [pending, setPending] = useState(0);
  const [workspace, setWorkspace] = useState<{ name: string; role: string } | null>(
    null
  );
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const active = location.pathname.split("/")[1] || "queue";

  /**
   * 待批数是**整个界面唯一的外部计数轮询**。
   *
   * 只问 `/tickets/pending` 的 count，不拉队列：一张挂起的工单可能等人批一整天，
   * 而坐席不该靠"恰好刷新了页面"才发现它。铃铛那边的未读轮询顺带驱动后端的
   * 线上健康评估，两件事合起来就够了，不需要再来一个定时器。
   */
  useEffect(() => {
    let alive = true;
    const load = () =>
      apiClient
        .getPendingApprovals()
        .then((r) => alive && setPending(r.count))
        .catch(() => undefined);
    load();
    const timer = window.setInterval(load, 30_000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [location.pathname]);

  useEffect(() => {
    apiClient
      .getWorkspace()
      .then((w) => setWorkspace({ name: w.name, role: w.role }))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  const handleLogout = async () => {
    try {
      await apiClient.logout();
    } catch {
      /* 服务端登出失败也要清本地：留着一枚已经作废的 token 只会让下次启动更慢 */
    }
    dispatch(clearAuth());
    navigate("/login");
  };

  const displayName = user?.name || user?.username || user?.email || "用户";
  const initial = displayName.charAt(0).toUpperCase();

  return (
    <aside className="w-[76px] shrink-0 h-full flex flex-col items-center py-4 gap-3 border-r border-line bg-mantle relative z-20">
      <button
        onClick={() => navigate("/queue")}
        className="transition-transform hover:scale-105"
        title="客服工单台"
        aria-label="客服工单台"
      >
        <BrandMark size={34} />
      </button>

      <nav className="flex-1 flex flex-col items-center gap-1.5 w-full">
        {MODULES.map((module) => {
          const on = active === module.id;
          const Icon = module.icon;
          const badge = module.alarm === "approvals" ? pending : 0;
          return (
            <button
              key={module.id}
              onClick={() => navigate(module.path)}
              className={`group relative w-11 h-11 rounded-xl flex items-center justify-center transition-all duration-200 ${
                on
                  ? "bg-surface text-accent shadow-card"
                  : "text-ink-soft hover:text-ink hover:bg-overlay"
              }`}
              aria-label={module.label}
              aria-current={on ? "page" : undefined}
            >
              {on && (
                <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 rounded-full bg-accent" />
              )}
              <Icon className="w-[18px] h-[18px]" />
              {badge > 0 && (
                <span className="absolute -top-1 -right-1 min-w-[17px] h-[17px] px-1 rounded-full bg-state-bad text-surface text-[9px] font-bold font-mono flex items-center justify-center">
                  {badge > 99 ? "99+" : badge}
                </span>
              )}
              <span className="pointer-events-none absolute left-[54px] px-2 py-1 rounded-md bg-ink text-surface text-[11px] font-medium whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity z-30 shadow-card">
                {module.label}
              </span>
            </button>
          );
        })}
      </nav>

      <div className="flex flex-col items-center gap-1.5 w-full pt-3 border-t border-line">
        <button
          onClick={() => navigate("/workspace")}
          className={`group relative w-11 h-11 rounded-xl flex items-center justify-center transition-all ${
            active === "settings"
              ? "bg-surface text-accent"
              : "text-ink-soft hover:text-ink hover:bg-overlay"
          }`}
          aria-label="工作区设置"
        >
          <Settings2 className="w-[18px] h-[18px]" />
          <span className="pointer-events-none absolute left-[54px] px-2 py-1 rounded-md bg-ink text-surface text-[11px] whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity z-30">
            设置
          </span>
        </button>

        <ThemeButton mode={mode} theme={theme} onCycle={cycleMode} />

        <NotificationBell />

        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen((open) => !open)}
            className="w-9 h-9 rounded-full bg-accent text-surface text-xs font-bold flex items-center justify-center mt-0.5 hover:brightness-110 transition"
            aria-label="账户菜单"
            aria-expanded={menuOpen}
          >
            {initial}
          </button>
          {menuOpen && (
            <div className="absolute bottom-0 left-[54px] w-60 card-surface rounded-xl p-3 z-40 anim-fade-up">
              <div className="text-sm font-semibold text-ink truncate">{displayName}</div>
              {user?.email && (
                <div className="text-[11px] text-ink-faint truncate">{user.email}</div>
              )}
              {workspace && (
                <div className="mt-2 pt-2 border-t border-line flex items-center justify-between gap-2">
                  <span className="text-[11px] text-ink-soft truncate">
                    {workspace.name}
                  </span>
                  <span className="chip text-[9px]">
                    {workspace.role === "admin" ? "管理员" : "成员"}
                  </span>
                </div>
              )}
              <button
                onClick={handleLogout}
                className="mt-3 w-full py-1.5 rounded-lg border text-[11px] font-semibold flex items-center justify-center gap-1.5 text-state-bad"
                style={{ borderColor: "var(--c-bad-faint)" }}
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

/**
 * 三态而不是两态：值班的机房是暗的，白天办公室是亮的，而公司的系统策略
 * 已经替很多人做过这个决定——"跟随系统"是把那个决定还给他们。
 *
 * 图标显示的是**当前生效**的那一套（跟随系统时跟着系统变），
 * 悬停提示说的是**下一次点击**会变成什么，两者不同是刻意的。
 */
const ThemeButton: React.FC<{
  mode: ThemeMode;
  theme: "light" | "dark";
  onCycle: () => void;
}> = ({ mode, theme, onCycle }) => {
  const Icon = mode === "system" ? Monitor : theme === "dark" ? Moon : Sun;
  const next = THEME_LABELS[NEXT_MODE[mode]];
  return (
    <button
      onClick={onCycle}
      className="group relative w-11 h-11 rounded-xl flex items-center justify-center text-ink-soft hover:text-ink hover:bg-overlay transition-all"
      aria-label={`主题：${THEME_LABELS[mode]}（点击切换到${next}）`}
    >
      <Icon className="w-[18px] h-[18px]" />
      <span className="pointer-events-none absolute left-[54px] px-2 py-1 rounded-md bg-ink text-surface text-[11px] whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity z-30">
        {THEME_LABELS[mode]} → {next}
      </span>
    </button>
  );
};
