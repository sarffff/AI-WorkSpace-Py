import React, { useEffect, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useNavigate } from "react-router-dom";
import { RootState } from "@/app/providers/store";
import {
  setSessions,
  setCurrentChat,
  deleteChat as deleteChatAction,
  togglePinChat as togglePinAction,
  renameChat as renameAction,
} from "@/entities/chat/model/chatSlice";
import { apiClient } from "@/shared/api/client";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import { Plus, Pin, PinOff, Trash2, Pencil, Download } from "lucide-react";

/**
 * 对话模块的上下文面板：会话列表 + 新对话。
 *
 * 从旧的全局侧栏搬过来——会话历史是「对话」这一个模块的上下文，不该占据全应用的
 * 最高导航层级。逻辑(getChats/rename/delete/pin + 乐观更新)照搬，不改行为。
 */
export const ChatListPanel: React.FC = () => {
  const dispatch = useDispatch();
  const navigate = useNavigate();
  const toast = useToast();
  const { currentChatId, sessions } = useSelector(
    (state: RootState) => state.chat,
  );
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

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
      .catch((e) => toast.error(toastMessageFrom(e, "加载会话列表失败")));
  }, [dispatch, toast]);

  const handleNewChat = () => {
    dispatch(setCurrentChat(null));
    navigate("/chat");
  };

  const handleSelectChat = (id: string) => {
    dispatch(setCurrentChat(id));
    navigate("/chat");
  };

  const handleSaveRename = (id: string) => {
    const trimmed = editValue.trim();
    if (trimmed) {
      const previousTitle = sessions.find((c) => c.id === id)?.title ?? trimmed;
      dispatch(renameAction({ id, title: trimmed }));
      apiClient.renameChat(id, trimmed).catch((e) => {
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

  // 导出整段对话为 Markdown（B5）。后端带 Content-Disposition 直接吐文件正文，
  // 这里把 Blob 变成一次浏览器下载。JSON 导出端点也支持（format=json），列表这里
  // 先只给最常用的 Markdown，不塞满悬浮操作行。
  const handleExport = async (id: string) => {
    try {
      const blob = await apiClient.exportChat(id, "md");
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `chat-${id}.md`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(toastMessageFrom(e, "导出失败"));
    }
  };

  const sorted = [...sessions].sort((a, b) => {
    if (a.pinned && !b.pinned) return -1;
    if (!a.pinned && b.pinned) return 1;
    return 0;
  });

  return (
    <div className="flex flex-col h-full">
      <div className="p-4 pb-3">
        <div className="label-eyebrow mb-2">对话 · 审核</div>
        <button
          onClick={handleNewChat}
          className="btn-accent w-full py-2.5 px-3.5 rounded-xl text-white text-xs font-medium flex items-center justify-center gap-2"
        >
          <Plus className="w-4 h-4" />
          开启新对话
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-3 pb-3">
        <div className="label-eyebrow px-2 mb-2">近期对话</div>
        <div className="space-y-0.5">
          {sorted.length === 0 && (
            <p className="px-2 py-8 text-center text-[11px] text-[#918d83] leading-relaxed">
              还没有对话。开启一个，让 Agent 按作业指导审一份材料。
            </p>
          )}
          {sorted.map((chat) => (
            <div
              key={chat.id}
              className={`group relative flex items-center gap-1.5 pl-3 pr-2 py-2 rounded-xl text-xs transition-all duration-200 cursor-pointer ${
                currentChatId === chat.id
                  ? "bg-[#eae6db] dark:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] font-medium shadow-sm"
                  : "text-[#52504a] dark:text-[#b0aea5] hover:bg-[#eae6db]/50 dark:hover:bg-[#22211e] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
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
                      handleExport(chat.id);
                    }}
                    className="p-1 rounded hover:bg-[#dcd7cb] dark:hover:bg-[#33312d] text-[#6e6b63] dark:text-[#a19f96] hover:text-[#da7756] transition-colors"
                    title="导出 Markdown"
                    aria-label="导出对话"
                  >
                    <Download className="w-3 h-3" />
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      dispatch(togglePinAction(chat.id));
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
                      setEditingId(chat.id);
                      setEditValue(chat.title);
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
    </div>
  );
};
