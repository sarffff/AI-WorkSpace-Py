import React, { useState, useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useNavigate, useLocation } from "react-router-dom";
import { RootState } from "@/app/providers/store";
import {
  setSessions,
  setCurrentChat,
  deleteChat as deleteChatAction,
  togglePinChat as togglePinAction,
  renameChat as renameAction,
} from "@/entities/chat/model/chatSlice";
import { clearAuth } from "@/entities/auth/model/authSlice";
import { apiClient } from "@/shared/api/client";
import {
  BookOpen,
  Sparkles,
  Settings,
  Plus,
  Bot,
  Database,
  Cpu,
  Pin,
  PinOff,
  Trash2,
  Pencil,
  LogOut,
  MessageSquare,
} from "lucide-react";

export const Sidebar: React.FC = () => {
  const dispatch = useDispatch();
  const navigate = useNavigate();
  const location = useLocation();
  const { currentChatId, sessions, selectedModel } = useSelector(
    (state: RootState) => state.chat,
  );
  const { user } = useSelector((state: RootState) => state.auth);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

  // 根据当前路径确定active tab
  const activeTab = location.pathname.split("/")[1] || "chat";

  // 初始加载时从服务器获取会话列表
  useEffect(() => {
    apiClient
      .getChats()
      .then((chats) => {
        dispatch(
          setSessions(
            chats.map((c) => ({
              id: c.id,
              title: c.title,
              date: new Date(c.createdAt).toLocaleString(),
              pinned: false,
            })),
          ),
        );
      })
      .catch(() => {});
  }, [dispatch]);

  const navItems = [
    {
      id: "chat",
      label: "对话",
      icon: <MessageSquare className="w-4 h-4" />,
      path: "/chat",
    },
    {
      id: "knowledge",
      label: "知识库",
      icon: <BookOpen className="w-4 h-4" />,
      path: "/knowledge",
    },
    {
      id: "prompts",
      label: "提示词",
      icon: <Sparkles className="w-4 h-4" />,
      path: "/prompts",
    },
    {
      id: "settings",
      label: "设置",
      icon: <Settings className="w-4 h-4" />,
      path: "/settings",
    },
  ];

  const handleNewChat = () => {
    dispatch(setCurrentChat(null));
    navigate("/chat");
  };

  const handleSelectChat = (id: string) => {
    dispatch(setCurrentChat(id));
    navigate("/chat");
  };

  const handleStartRename = (id: string, title: string) => {
    setEditingId(id);
    setEditValue(title);
  };

  const handleSaveRename = (id: string) => {
    const trimmed = editValue.trim();
    if (trimmed) {
      dispatch(renameAction({ id, title: trimmed }));
      apiClient.renameChat(id, trimmed).catch(() => {});
    }
    setEditingId(null);
    setEditValue("");
  };

  const handleDelete = (id: string) => {
    dispatch(deleteChatAction(id));
    apiClient.deleteChat(id).catch(() => {});
  };

  const handleTogglePin = (id: string) => {
    dispatch(togglePinAction(id));
  };

  const handleLogout = async () => {
    try {
      await apiClient.logout();
    } catch (error) {
      console.error("Logout error:", error);
    } finally {
      dispatch(clearAuth());
      navigate("/login");
    }
  };

  const sorted = [...sessions].sort((a, b) => {
    if (a.pinned && !b.pinned) return -1;
    if (!a.pinned && b.pinned) return 1;
    return 0;
  });

  return (
    <aside className="w-64 bg-slate-900/80 border-r border-slate-800/80 flex flex-col h-full select-none">
      <div className="p-4 flex items-center gap-3 border-b border-slate-800/60">
        <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-indigo-600 via-blue-500 to-cyan-400 flex items-center justify-center shadow-lg shadow-indigo-500/20">
          <Bot className="w-5 h-5 text-white" />
        </div>
        <div>
          <h1 className="font-semibold text-sm text-slate-100 tracking-wide">
            AI 工作区
          </h1>
          <span className="text-[11px] text-slate-400 flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
            {user?.name || user?.username || "User"}
          </span>
        </div>
      </div>

      <div className="p-3">
        <button
          onClick={handleNewChat}
          className="w-full py-2 px-3 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium flex items-center justify-center gap-2 transition-all shadow-md shadow-indigo-600/20"
        >
          <Plus className="w-4 h-4" />
          新建聊天
        </button>
      </div>

      <div className="px-3 py-2 space-y-1">
        <div className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold px-2 mb-1">
          导航栏
        </div>
        {navItems.map((item) => (
          <button
            key={item.id}
            onClick={() => navigate(item.path)}
            className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-xs font-medium transition-colors ${
              activeTab === item.id
                ? "bg-slate-800 text-slate-100 border border-slate-700/60"
                : "text-slate-400 hover:text-slate-200 hover:bg-slate-800/50"
            }`}
          >
            {item.icon}
            {item.label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2 mt-2">
        <div className="text-[10px] uppercase tracking-wider text-slate-500 font-semibold px-2 mb-2">
          最近聊天列表
        </div>
        <div className="space-y-1">
          {sorted.map((chat) => (
            <div
              key={chat.id}
              className={`group relative flex items-center gap-1 px-3 py-2 rounded-lg text-xs transition-colors cursor-pointer ${
                currentChatId === chat.id
                  ? "bg-slate-800 text-slate-100 border border-slate-700/60"
                  : "text-slate-300 hover:bg-slate-800/50 hover:text-slate-100"
              }`}
              onClick={() => handleSelectChat(chat.id)}
            >
              {chat.pinned && (
                <Pin className="w-3 h-3 text-amber-400 shrink-0 fill-amber-400" />
              )}

              {editingId === chat.id ? (
                <input
                  className="flex-1 bg-slate-700 text-xs text-slate-100 px-1 py-0.5 rounded border border-indigo-500 outline-none min-w-0"
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleSaveRename(chat.id);
                    if (e.key === "Escape") setEditingId(null);
                  }}
                  onBlur={() => handleSaveRename(chat.id)}
                  autoFocus
                  onClick={(e) => e.stopPropagation()}
                />
              ) : (
                <span className="flex-1 truncate font-medium">
                  {chat.title}
                </span>
              )}

              {editingId !== chat.id && (
                <div className="hidden group-hover:flex items-center gap-0.5 shrink-0">
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleTogglePin(chat.id);
                    }}
                    className="p-1 rounded hover:bg-slate-700 text-slate-400 hover:text-amber-400 transition-colors"
                    title={chat.pinned ? "取消固定" : "固定"}
                  >
                    {chat.pinned ? (
                      <PinOff className="w-3 h-3" />
                    ) : (
                      <Pin className="w-3 h-3" />
                    )}
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleStartRename(chat.id, chat.title);
                    }}
                    className="p-1 rounded hover:bg-slate-700 text-slate-400 hover:text-slate-100 transition-colors"
                    title="重命名"
                  >
                    <Pencil className="w-3 h-3" />
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDelete(chat.id);
                    }}
                    className="p-1 rounded hover:bg-slate-700 text-slate-400 hover:text-red-400 transition-colors"
                    title="删除"
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="p-3 m-3 rounded-xl bg-slate-950/60 border border-slate-800/80 space-y-2">
        <div className="flex items-center gap-2 text-xs font-medium text-slate-300">
          <Cpu className="w-3.5 h-3.5 text-cyan-400" />
          <span>当前提供程序</span>
        </div>
        <div className="text-[11px] text-slate-400 flex items-center justify-between">
          <span>模型</span>
          <span className="text-slate-200 font-mono text-[10px] px-1.5 py-0.5 bg-slate-800 rounded">
            {selectedModel}
          </span>
        </div>
        <div className="text-[11px] text-slate-400 flex items-center justify-between">
          <span>数据库</span>
          <span className="text-slate-200 font-mono text-[10px] px-1.5 py-0.5 bg-slate-800 rounded flex items-center gap-1">
            <Database className="w-2.5 h-2.5 text-emerald-400" /> MySQL + Redis
          </span>
        </div>
        <button
          onClick={handleLogout}
          className="w-full mt-2 py-1.5 px-3 rounded-lg bg-red-600/10 hover:bg-red-600/20 text-red-400 text-xs font-medium flex items-center justify-center gap-2 transition-colors border border-red-600/20"
        >
          <LogOut className="w-3.5 h-3.5" />
          登出
        </button>
      </div>
    </aside>
  );
};
