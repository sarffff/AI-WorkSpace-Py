import React, { useCallback, useEffect, useRef, useState } from "react";
import { useDispatch } from "react-redux";
import { useNavigate } from "react-router-dom";
import {
  Activity,
  Bell,
  CheckCheck,
  Clock,
  HelpCircle,
  Inbox,
  Loader2,
  ShieldAlert,
} from "lucide-react";
import { apiClient } from "@/shared/api/client";
import { setCurrentChat } from "@/entities/chat/model/chatSlice";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import { fmtRelativeTime } from "@/shared/lib/format";
import type { AppNotification } from "@/shared/types/api.types";

/**
 * 顶栏通知铃铛 + 未读红点 + 下拉收件箱（补 B1 的洞——后端早已上线、前端此前零消费）。
 *
 * 轮询 `/notifications/unread_count` 刷新红点，这同时是**线上健康监控的心跳**：后端
 * 在那个端点里顺带跑 evaluate_and_alert（自带节流 + 非阻塞兜底，关着时空转），于是
 * 不引调度器也能让告警"主动"推给管理员。所以这颗铃铛不只是收件箱，也替整个部署驱动
 * 健康评估——轮询失败只静默（后台心跳不该弹 toast 打扰人）。
 *
 * HITL 闭环的最后一环：审批/澄清挂起后，审批人若不盯着那一个会话页就不知道有事等他。
 * 点一条带 chatId 的通知 → 切到那个会话 → 待审批/待回答卡片在 ChatPage 里重新浮出。
 */
const POLL_MS = 45_000;

// kind → 图标 + 类别名。未登记的类别回落到通用铃铛（后端可能加新类别，前端不认时
// 也要能渲染而不是空白）。approval/input 是要人动手的，用强调色；超时是记录，用灰；
// 健康告警是运营事故，用警示红。
const KIND_META: Record<
  string,
  { icon: React.ComponentType<{ className?: string }>; label: string; tone: string }
> = {
  approval_required: { icon: ShieldAlert, label: "待审批", tone: "text-[#da7756]" },
  input_required: { icon: HelpCircle, label: "待回答", tone: "text-[#da7756]" },
  run_abandoned: { icon: Clock, label: "已超时", tone: "text-[#918d83]" },
  health_alert: { icon: Activity, label: "健康告警", tone: "text-rose-500" },
};

const metaFor = (kind: string) =>
  KIND_META[kind] ?? { icon: Bell, label: "通知", tone: "text-[#6e6b63] dark:text-[#a19f96]" };

export const NotificationBell: React.FC = () => {
  const dispatch = useDispatch();
  const navigate = useNavigate();
  const toast = useToast();

  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(0);
  const [items, setItems] = useState<AppNotification[]>([]);
  const [loading, setLoading] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // 红点轮询：挂载即查一次，之后每 POLL_MS 一次。失败静默——离线/刷新 token 失败时
  // 不该每 45 秒弹一次红字。顺带驱动后端健康心跳（见文件头）。
  useEffect(() => {
    let alive = true;
    const tick = () => {
      apiClient
        .getUnreadCount()
        .then((n) => {
          if (alive) setUnread(n);
        })
        .catch(() => {});
    };
    tick();
    const timer = window.setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  // 点面板外收起
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const loadList = useCallback(() => {
    setLoading(true);
    apiClient
      .getNotifications(false, 20)
      .then((res) => {
        setItems(res.notifications);
        setUnread(res.unreadCount); // 以列表响应为准，顺手校正红点
      })
      .catch((e) => toast.error(toastMessageFrom(e, "加载通知失败")))
      .finally(() => setLoading(false));
  }, [toast]);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) loadList(); // 打开才拉正文，平时只轮询数字
  };

  // 点一条：先乐观标记已读（红点立刻降），再按 chatId 跳到对应会话去处置。
  // 带 chatId 的（审批/回答/超时）跳对话；health_alert 没有 chatId，只标已读不跳转，
  // 详情就在 body 里。标记失败不回滚：对用户来说"已读"是无害的乐观操作。
  const handleOpenItem = (note: AppNotification) => {
    if (!note.read) {
      setItems((prev) =>
        prev.map((n) => (n.id === note.id ? { ...n, read: true } : n)),
      );
      setUnread((n) => Math.max(0, n - 1));
      apiClient.markNotificationRead(note.id).catch(() => {});
    }
    if (note.chatId) {
      dispatch(setCurrentChat(note.chatId));
      navigate("/chat");
      setOpen(false);
    }
  };

  const handleMarkAll = () => {
    if (unread === 0) return;
    setItems((prev) => prev.map((n) => ({ ...n, read: true })));
    setUnread(0);
    apiClient
      .markAllNotificationsRead()
      .catch((e) => {
        toast.error(toastMessageFrom(e, "标记已读失败"));
        loadList(); // 失败就把真实状态拉回来，别让界面骗人
      });
  };

  return (
    <div className="relative" ref={panelRef}>
      <button
        onClick={toggle}
        className="relative w-9 h-9 rounded-full flex items-center justify-center text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#f3f0e6] dark:hover:bg-[#1e1d1b] transition-colors"
        aria-label={unread > 0 ? `通知（${unread} 条未读）` : "通知"}
        aria-expanded={open}
      >
        <Bell className="w-[18px] h-[18px]" />
        {unread > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 rounded-full bg-[#da7756] text-white text-[9px] font-bold flex items-center justify-center">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-[46px] w-[340px] card-surface rounded-xl z-50 anim-fade-up overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-[#e6e2d8] dark:border-[#282724]">
            <div>
              <div className="label-eyebrow leading-none mb-0.5">Inbox</div>
              <div className="font-display text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                通知
              </div>
            </div>
            <button
              onClick={handleMarkAll}
              disabled={unread === 0}
              className="flex items-center gap-1 text-[11px] text-[#6e6b63] dark:text-[#a19f96] hover:text-[#da7756] disabled:opacity-40 disabled:hover:text-[#6e6b63] transition-colors"
            >
              <CheckCheck className="w-3.5 h-3.5" />
              全部已读
            </button>
          </div>

          <div className="max-h-[380px] overflow-y-auto">
            {/* LIST_MARKER */}
            {loading ? (
              <div className="flex items-center justify-center py-10 text-[#918d83]">
                <Loader2 className="w-4 h-4 animate-spin" />
              </div>
            ) : items.length === 0 ? (
              <div className="flex flex-col items-center gap-2 py-12 text-center">
                <Inbox className="w-7 h-7 text-[#c9c3b5] dark:text-[#4a473f]" />
                <p className="text-[11px] text-[#918d83]">暂无通知</p>
              </div>
            ) : (
              items.map((note) => {
                const meta = metaFor(note.kind);
                const Icon = meta.icon;
                const clickable = !!note.chatId;
                return (
                  <button
                    key={note.id}
                    onClick={() => handleOpenItem(note)}
                    className={`w-full text-left flex gap-2.5 px-4 py-3 border-b border-[#f0ece2] dark:border-[#232220] last:border-0 transition-colors ${
                      clickable
                        ? "cursor-pointer hover:bg-[#f3f0e6]/60 dark:hover:bg-[#1e1d1b]"
                        : "cursor-default"
                    } ${note.read ? "" : "bg-[#da7756]/[0.04]"}`}
                  >
                    <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${meta.tone}`} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="chip text-[9px]">{meta.label}</span>
                        {!note.read && (
                          <span className="w-1.5 h-1.5 rounded-full bg-[#da7756]" />
                        )}
                        <span className="ml-auto text-[10px] text-[#918d83] shrink-0">
                          {fmtRelativeTime(note.createdAt)}
                        </span>
                      </div>
                      <div
                        className={`mt-1 text-xs leading-snug truncate ${
                          note.read
                            ? "text-[#6e6b63] dark:text-[#a19f96]"
                            : "text-[#1f1e1d] dark:text-[#edece8] font-medium"
                        }`}
                      >
                        {note.title}
                      </div>
                      {note.body && (
                        <div className="mt-0.5 text-[11px] text-[#918d83] line-clamp-2 break-words">
                          {note.body}
                        </div>
                      )}
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
};
