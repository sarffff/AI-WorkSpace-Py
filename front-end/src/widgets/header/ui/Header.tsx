import React, { useEffect } from "react";
import { useSelector, useDispatch } from "react-redux";
import { RootState } from "@/app/providers/store";
import {
  setSelectedModel,
  setServerStatus,
} from "@/entities/chat/model/chatSlice";
import { apiClient } from "@/shared/api/client";
import { useTheme } from "@/shared/lib/ThemeContext";
import { ChevronDown, ShieldCheck, Sun, Moon, Sparkles } from "lucide-react";

export const Header: React.FC = () => {
  const dispatch = useDispatch();
  const { theme, toggleTheme } = useTheme();
  const { selectedModel, activeTab, serverStatus, sessions } = useSelector(
    (state: RootState) => state.chat,
  );
  const [appVersion] = React.useState<string>("0.1.0");
  const [models, setModels] = React.useState<string[]>([selectedModel]);

  useEffect(() => {
    apiClient.ping().then((online) => {
      dispatch(setServerStatus(online ? "online" : "offline"));
    });
    apiClient.getSettings().then((settings) => {
      const supported = settings.availableModels.map((model) => model.id);
      setModels(supported);
      if (!supported.includes(selectedModel)) {
        dispatch(setSelectedModel(settings.preferences.defaultModel));
      }
    }).catch(() => {});
  }, [dispatch, selectedModel]);

  const titles: Record<string, string> = {
    dashboard: "工作台",
    chat: "智能对话",
    traces: "运行轨迹",
    knowledge: "知识库",
    prompts: "提示词工作台",
    settings: "设置",
  };

  const dotClass =
    serverStatus === "online"
      ? "bg-emerald-500"
      : serverStatus === "offline"
        ? "bg-rose-500"
        : "bg-amber-500";

  const statusLabel =
    serverStatus === "online"
      ? `在线（${sessions.length} 个会话）`
      : serverStatus === "offline"
        ? "服务离线"
        : "正在检查服务...";

  return (
    <header className="h-14 border-b border-[#e6e2d8] dark:border-[#282724] bg-[#fbf9f5]/75 dark:bg-[#141413]/75 backdrop-blur-md px-6 flex items-center justify-between select-none transition-colors duration-200 relative z-10">
      <div className="flex items-center gap-3">
        <h2 className="font-display text-[17px] font-semibold text-[#1f1e1d] dark:text-[#edece8]">
          {titles[activeTab] || "工作台"}
        </h2>
        <span
          className="chip chip-accent"
          style={{ fontFamily: "var(--font-mono)" }}
        >
          v{appVersion}
        </span>
      </div>

      <div className="flex items-center gap-2.5">
        {/* Model Selector Pill */}
        <div className="relative group">
          <Sparkles className="w-3.5 h-3.5 text-[#da7756] absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
          <select
            value={selectedModel}
            onChange={(e) => dispatch(setSelectedModel(e.target.value))}
            className="appearance-none bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#1e1d1b] dark:hover:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] text-xs font-medium rounded-full pl-8 pr-8 py-1.5 border border-[#e3dfd5] dark:border-[#2e2d2a] hover:border-[#da7756]/40 focus:outline-none focus:ring-1 focus:ring-[#da7756] cursor-pointer transition-all shadow-sm"
          >
            {models.map((m) => (
              <option
                key={m}
                value={m}
                className="bg-[#fbf9f5] dark:bg-[#181816] text-[#1f1e1d] dark:text-[#edece8]"
              >
                {m}
              </option>
            ))}
          </select>
          <ChevronDown className="w-3.5 h-3.5 text-[#6e6b63] dark:text-[#a19f96] absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none" />
        </div>

        {/* Server Status Pill */}
        <div className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-full bg-[#f3f0e6]/60 dark:bg-[#1e1d1b]/60 border border-[#e3dfd5] dark:border-[#2e2d2a] text-[11px] text-[#6e6b63] dark:text-[#a19f96]">
          <span className="relative flex w-2 h-2">
            {serverStatus === "online" && (
              <span
                className={`animate-ping absolute inline-flex h-full w-full rounded-full ${dotClass} opacity-50`}
              />
            )}
            <span
              className={`relative inline-flex rounded-full h-2 w-2 ${dotClass}`}
            />
          </span>
          <ShieldCheck className="w-3.5 h-3.5" />
          <span>{statusLabel}</span>
        </div>

        {/* Theme Toggle Button */}
        <button
          onClick={toggleTheme}
          className="p-2 rounded-full bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#1e1d1b] dark:hover:bg-[#262522] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#1f1e1d] dark:text-[#edece8] transition-all duration-300 shadow-sm hover:rotate-[20deg] hover:border-[#da7756]/40"
          title={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
        >
          {theme === "dark" ? (
            <Sun className="w-4 h-4 text-amber-400" />
          ) : (
            <Moon className="w-4 h-4 text-slate-700" />
          )}
        </button>
      </div>
    </header>
  );
};
