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

  const activeTab = location.pathname.split("/")[1] || "chat";

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
    } catch {
      // 即使服务端登出失败也继续清除前端状态
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
    <aside className="w-64 bg-[#f3f0e6] dark:bg-[#1a1917] border-r border-[#e6e2d8] dark:border-[#282724] flex flex-col h-full select-none transition-colors duration-200">
      {/* Brand & User Header */}
      <div className="p-4 flex items-center gap-3 border-b border-[#e6e2d8]/80 dark:border-[#282724]/80">
        <div className="w-9 h-9 rounded-xl bg-[#da7756] text-white flex items-center justify-center shadow-md shadow-[#da7756]/20">
          <Bot className="w-5 h-5" />
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="font-semibold text-sm text-[#1f1e1d] dark:text-[#edece8] tracking-tight truncate">
            AI Workspace
          </h1>
          <span className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] flex items-center gap-1.5 truncate">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shrink-0" />
            {user?.name || user?.username || "User"}
          </span>
        </div>
      </div>

      {/* New Chat Button */}
      <div className="p-3">
        <button
          onClick={handleNewChat}
          className="w-full py-2.5 px-3.5 rounded-xl bg-[#da7756] hover:bg-[#c86544] text-white text-xs font-medium flex items-center justify-center gap-2 transition-all shadow-md shadow-[#da7756]/20 active:scale-[0.99]"
        >
          <Plus className="w-4 h-4" />
          开启新对话
        </button>
      </div>

      {/* Navigation */}
      <div className="px-3 py-1 space-y-1">
        <div className="text-[10px] uppercase tracking-wider text-[#918d83] dark:text-[#78756d] font-bold px-2 mb-1">
          导航栏
        </div>
        {navItems.map((item) => (
          <button
            key={item.id}
            onClick={() => navigate(item.path)}
            className={`w-full flex items-center gap-3 px-3 py-2 rounded-xl text-xs font-medium transition-all ${
              activeTab === item.id
                ? "bg-[#eae6db] dark:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] font-semibold border border-[#dcd7cb] dark:border-[#33312d]"
                : "text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#eae6db]/60 dark:hover:bg-[#22211e]"
            }`}
          >
            {item.icon}
            {item.label}
          </button>
        ))}
      </div>

      {/* Recent Chats List */}
      <div className="flex-1 overflow-y-auto px-3 py-2 mt-2">
        <div className="text-[10px] uppercase tracking-wider text-[#918d83] dark:text-[#78756d] font-bold px-2 mb-2">
          近期对话
        </div>
        <div className="space-y-1">
          {sorted.map((chat) => (
            <div
              key={chat.id}
              className={`group relative flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs transition-all cursor-pointer ${
                currentChatId === chat.id
                  ? "bg-[#eae6db] dark:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] font-medium border border-[#dcd7cb] dark:border-[#33312d]"
                  : "text-[#52504a] dark:text-[#b0aeA5] hover:bg-[#eae6db]/50 dark:hover:bg-[#22211e] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
              }`}
              onClick={() => handleSelectChat(chat.id)}
            >
              {chat.pinned && (
                <Pin className="w-3 h-3 text-[#da7756] shrink-0 fill-[#da7756]" />
              )}

              {editingId === chat.id ? (
                <input
                  className="flex-1 bg-white dark:bg-[#2b2a27] text-xs text-[#1f1e1d] dark:text-[#edece8] px-1.5 py-0.5 rounded-md border border-[#da7756] outline-none min-w-0"
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
                <span className="flex-1 truncate">
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
                    className="p-1 rounded hover:bg-[#dcd7cb] dark:hover:bg-[#33312d] text-[#6e6b63] dark:text-[#a19f96] hover:text-[#da7756] transition-colors"
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
                    className="p-1 rounded hover:bg-[#dcd7cb] dark:hover:bg-[#33312d] text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] transition-colors"
                    title="重命名"
                  >
                    <Pencil className="w-3 h-3" />
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDelete(chat.id);
                    }}
                    className="p-1 rounded hover:bg-[#dcd7cb] dark:hover:bg-[#33312d] text-[#6e6b63] dark:text-[#a19f96] hover:text-rose-600 transition-colors"
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

      {/* Bottom Status Card */}
      <div className="p-3 m-3 rounded-xl bg-[#eae6db]/60 dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2a2926] space-y-2">
        <div className="flex items-center gap-2 text-xs font-medium text-[#1f1e1d] dark:text-[#edece8]">
          <Cpu className="w-3.5 h-3.5 text-[#da7756]" />
          <span>服务提供节点</span>
        </div>
        <div className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] flex items-center justify-between">
          <span>当前模型</span>
          <span className="text-[#1f1e1d] dark:text-[#edece8] font-mono text-[10px] px-1.5 py-0.5 bg-[#dcd7cb] dark:bg-[#2b2a27] rounded-md font-semibold">
            {selectedModel}
          </span>
        </div>
        <div className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] flex items-center justify-between">
          <span>存储</span>
          <span className="text-[#1f1e1d] dark:text-[#edece8] font-mono text-[10px] px-1.5 py-0.5 bg-[#dcd7cb] dark:bg-[#2b2a27] rounded-md flex items-center gap-1">
            <Database className="w-2.5 h-2.5 text-emerald-500" /> MySQL + Redis
          </span>
        </div>
        <button
          onClick={handleLogout}
          className="w-full mt-2 py-1.5 px-3 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-600 dark:text-rose-400 text-xs font-medium flex items-center justify-center gap-2 transition-colors border border-rose-500/20"
        >
          <LogOut className="w-3.5 h-3.5" />
          退出登录
        </button>
      </div>
    </aside>
  );
};
