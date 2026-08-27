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
import { BrandMark } from "@/shared/ui/BrandMark";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import {
  BookOpen,
  Sparkles,
  Settings,
  Plus,
  Pin,
  PinOff,
  Trash2,
  Pencil,
  LogOut,
  LayoutDashboard,
  Route,
} from "lucide-react";

export const Sidebar: React.FC = () => {
  const dispatch = useDispatch();
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();
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
      .catch((e) => {
        toast.error(toastMessageFrom(e, "加载会话列表失败"));
      });
  }, [dispatch, toast]);

  const navGroups = [
    {
      label: "工作区",
      items: [
        {
          id: "dashboard",
          label: "工作台",
          icon: <LayoutDashboard className="w-4 h-4" />,
          path: "/dashboard",
        },
        // { id: "chat", label: "对话", icon: <MessageSquare className="w-4 h-4" />, path: "/chat" },
        {
          id: "traces",
          label: "运行轨迹",
          icon: <Route className="w-4 h-4" />,
          path: "/traces",
        },
      ],
    },
    {
      label: "资源",
      items: [
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
      ],
    },
    {
      label: "系统",
      items: [
        {
          id: "settings",
          label: "设置",
          icon: <Settings className="w-4 h-4" />,
          path: "/settings",
        },
      ],
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
      const previousTitle = sessions.find((c) => c.id === id)?.title ?? trimmed;
      dispatch(renameAction({ id, title: trimmed }));
      apiClient.renameChat(id, trimmed).catch((e) => {
        // 乐观更新失败时把标题滚回去，侧栏不能和后端悄悄分叉
        dispatch(renameAction({ id, title: previousTitle }));
        toast.error(toastMessageFrom(e, "重命名失败"));
      });
    }
    setEditingId(null);
    setEditValue("");
  };

  const handleDelete = (id: string) => {
    dispatch(deleteChatAction(id));
    apiClient
      .deleteChat(id)
      .catch((e) =>
        toast.error(toastMessageFrom(e, "删除失败，请刷新页面后重试")),
      );
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
    <aside className="w-64 bg-[#f3f0e6]/90 dark:bg-[#1a1917]/90 backdrop-blur-sm border-r border-[#e6e2d8] dark:border-[#282724] flex flex-col h-full select-none transition-colors duration-200 relative z-10">
      <div className="p-4 flex items-center gap-3 border-b border-[#e6e2d8]/80 dark:border-[#282724]/80">
        <BrandMark size={36} />
        <div className="min-w-0 flex-1">
          <h1 className="font-display font-semibold text-[15px] text-[#1f1e1d] dark:text-[#edece8] truncate">
            有据工作台
          </h1>
          <span className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] flex items-center gap-1.5 truncate">
            <span className="relative flex w-1.5 h-1.5 shrink-0">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-60" />
              <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500" />
            </span>
            {user?.name || user?.username || "用户"}
          </span>
        </div>
      </div>

      {/* New Chat Button */}
      <div className="p-3">
        <button
          onClick={handleNewChat}
          className="btn-accent w-full py-2.5 px-3.5 rounded-xl text-white text-xs font-medium flex items-center justify-center gap-2"
        >
          <Plus className="w-4 h-4" />
          开启新对话
        </button>
      </div>

      {/* Navigation */}
      <div className="px-3 py-1 space-y-3">
        {navGroups.map((group) => (
          <div key={group.label}>
            <div className="label-eyebrow px-2 mb-1.5">{group.label}</div>
            {group.items.map((item) => (
              <button
                key={item.id}
                onClick={() => navigate(item.path)}
                className={`relative w-full flex items-center gap-3 px-3 py-2 rounded-xl text-xs font-medium transition-all duration-200 ${
                  activeTab === item.id
                    ? "bg-[#eae6db] dark:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] font-semibold shadow-sm"
                    : "text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#eae6db]/60 dark:hover:bg-[#22211e]"
                }`}
              >
                {activeTab === item.id && (
                  <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 rounded-full bg-[#da7756]" />
                )}
                <span
                  className={
                    activeTab === item.id ? "text-[#da7756]" : "text-current"
                  }
                >
                  {item.icon}
                </span>
                {item.label}
              </button>
            ))}
          </div>
        ))}
      </div>

      {/* Recent Chats List */}
      <div className="flex-1 overflow-y-auto px-3 py-2 mt-2">
        <div className="label-eyebrow px-2 mb-2">近期对话</div>
        <div className="space-y-0.5">
          {sorted.map((chat) => (
            <div
              key={chat.id}
              className={`group relative flex items-center gap-1.5 pl-3 pr-2 py-2 rounded-xl text-xs transition-all duration-200 cursor-pointer ${
                currentChatId === chat.id
                  ? "bg-[#eae6db] dark:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] font-medium shadow-sm"
                  : "text-[#52504a] dark:text-[#b0aeA5] hover:bg-[#eae6db]/50 dark:hover:bg-[#22211e] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
              }`}
              onClick={() => handleSelectChat(chat.id)}
            >
              {currentChatId === chat.id && (
                <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 rounded-full bg-[#da7756]" />
              )}
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
                  aria-label="重命名会话"
                  onClick={(e) => e.stopPropagation()}
                />
              ) : (
                <span className="flex-1 truncate">{chat.title}</span>
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
                    aria-label={chat.pinned ? "取消固定会话" : "固定会话"}
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
                    aria-label="重命名会话"
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
                    aria-label="删除会话"
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="p-3.5 m-3 rounded-2xl bg-[#eae6db]/60 dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2a2926] space-y-3">
        <div className="space-y-1.5">
          <div className="label-eyebrow">当前模型</div>
          <div
            className="text-[11px] font-semibold text-[#1f1e1d] dark:text-[#edece8] truncate"
            style={{ fontFamily: "var(--font-mono)" }}
            title={selectedModel}
          >
            {selectedModel}
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {["检索", "轨迹", "护栏"].map((label) => (
            <span key={label} className="chip text-[9px] px-1.5 py-0.5">
              <span className="capability-dot !w-1.5 !h-1.5 !shadow-none" />
              {label}
            </span>
          ))}
        </div>
        <button
          onClick={handleLogout}
          className="w-full py-1.5 px-3 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-600 dark:text-rose-400 text-[11px] font-medium flex items-center justify-center gap-1.5 transition-colors border border-rose-500/20"
        >
          <LogOut className="w-3 h-3" />
          退出登录
        </button>
      </div>
    </aside>
  );
};
