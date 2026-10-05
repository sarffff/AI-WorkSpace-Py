import React, { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Activity,
  Bell,
  CheckCheck,
  Inbox,
  Loader2,
  ShieldAlert,
  Users,
} from "lucide-react";
import { apiClient } from "@/shared/api/client";
import { fmtRelativeTime } from "@/shared/lib/format";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import type { AppNotification } from "@/shared/types/api.types";

/**
 * 图标轨上的通知铃铛：红点 + 下拉收件箱。
 *
 * 轮询 `/notifications/unread_count` 不只是为了刷新红点——后端在那个端点里顺带跑
 * 一次线上健康评估（自带节流与非阻塞兜底）。也就是说**关掉这颗铃铛等于关掉部署的
 * 告警心跳**，所以轮询失败时静默处理，而不是停掉定时器。
 *
 * 一条通知的归属是 `ticketId` 而不是某个会话：这张单可能是一小时前挂起的，
 * 点开的正确动作是把那张单的轨迹摆出来。没有 ticketId 的（健康告警）指向指标看板。
 */
const POLL_MS = 45_000;

const KIND_META: Record<
  string,
  {
    icon: React.ComponentType<{ className?: string; style?: React.CSSProperties }>;
    label: string;
    varName: string;
  }
> = {
  approval_required: { icon: ShieldAlert, label: "等你批", varName: "--c-hold" },
  ticket_handoff: { icon: Users, label: "转人工", varName: "--c-bad" },
  health_alert: { icon: Activity, label: "健康告警", varName: "--c-bad" },
};

const metaFor = (kind: string) =>
  KIND_META[kind] ?? { icon: Bell, label: "通知", varName: "--c-ink-soft" };

export const NotificationBell: React.FC = () => {
  const navigate = useNavigate();
  const toast = useToast();

  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(0);
  const [items, setItems] = useState<AppNotification[]>([]);
  const [loading, setLoading] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    const tick = () =>
      apiClient
        .getUnreadCount()
        .then((count) => alive && setUnread(count))
        // 静默：后台心跳不该每 45 秒弹一次红字，网络抖一下不算事故
        .catch(() => undefined);
    tick();
    const timer = window.setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const loadList = useCallback(() => {
    setLoading(true);
    apiClient
      .getNotifications(false, 20)
      .then((res) => {
        setItems(res.notifications);
        // 以列表响应为准，顺手校正红点：两个数不一致时以服务端那个赢
        setUnread(res.unreadCount);
      })
      .catch((e) => toast.error(toastMessageFrom(e, "通知读取失败")))
      .finally(() => setLoading(false));
  }, [toast]);

  const openItem = (note: AppNotification) => {
    if (!note.read) {
      setItems((prev) => prev.map((n) => (n.id === note.id ? { ...n, read: true } : n)));
      setUnread((n) => Math.max(0, n - 1));
      apiClient.markNotificationRead(note.id).catch(() => undefined);
    }
    if (note.ticketId) {
      navigate(`/tickets/${note.ticketId}`);
      setOpen(false);
      return;
    }
    if (note.kind === "health_alert") {
      navigate("/metrics");
      setOpen(false);
    }
  };

  const markAll = () => {
    if (unread === 0) return;
    setItems((prev) => prev.map((n) => ({ ...n, read: true })));
    setUnread(0);
    apiClient.markAllNotificationsRead().catch((e) => {
      toast.error(toastMessageFrom(e, "标记已读失败"));
      // 拉回真实状态：本地清零而服务端没清，界面上就在骗人
      loadList();
    });
  };

  return (
    <div className="relative" ref={panelRef}>
      <button
        onClick={() => {
          const next = !open;
          setOpen(next);
          if (next) loadList();
        }}
        className="group relative w-11 h-11 rounded-xl flex items-center justify-center text-ink-soft hover:text-ink hover:bg-overlay transition-all"
        aria-label={unread > 0 ? `通知（${unread} 条未读）` : "通知"}
        aria-expanded={open}
      >
        <Bell className="w-[18px] h-[18px]" />
        {unread > 0 && (
          <span className="absolute -top-1 -right-1 min-w-[17px] h-[17px] px-1 rounded-full bg-state-hold text-surface text-[9px] font-bold font-mono flex items-center justify-center">
            {unread > 99 ? "99+" : unread}
          </span>
        )}
        <span className="pointer-events-none absolute left-[54px] px-2 py-1 rounded-md bg-ink text-surface text-[11px] whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity z-30">
          通知
        </span>
      </button>

      {open && (
        <div className="absolute bottom-0 left-[54px] w-[340px] card-surface rounded-xl z-50 anim-fade-up overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-line">
            <div>
              <div className="label-eyebrow leading-none mb-0.5">INBOX</div>
              <div className="font-display text-sm font-semibold text-ink">通知</div>
            </div>
            <button
              onClick={markAll}
              disabled={unread === 0}
              className="flex items-center gap-1 text-[11px] text-ink-soft hover:text-accent disabled:opacity-40"
            >
              <CheckCheck className="w-3.5 h-3.5" />
              全部已读
            </button>
          </div>

          <div className="max-h-[340px] overflow-y-auto">
            {loading ? (
              <div className="flex items-center justify-center py-10 text-ink-faint">
                <Loader2 className="w-4 h-4 animate-spin" />
              </div>
            ) : items.length === 0 ? (
              <div className="flex flex-col items-center gap-2 py-12 text-center">
                <Inbox className="w-7 h-7 text-ink-faint" />
                <p className="text-[11px] text-ink-faint">还没有通知</p>
              </div>
            ) : (
              items.map((note) => {
                const meta = metaFor(note.kind);
                const Icon = meta.icon;
                return (
                  <button
                    key={note.id}
                    onClick={() => openItem(note)}
                    className={`w-full text-left flex gap-2.5 px-4 py-3 border-b border-line last:border-0 transition-colors ${
                      note.ticketId || note.kind === "health_alert"
                        ? "cursor-pointer hover:bg-mantle"
                        : "cursor-default"
                    }`}
                    style={note.read ? undefined : { background: "var(--c-accent-faint)" }}
                  >
                    <Icon className="w-4 h-4 mt-0.5 shrink-0" style={{ color: `var(${meta.varName})` }} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <span className="chip text-[9px]">{meta.label}</span>
                        {!note.read && (
                          <span className="w-1.5 h-1.5 rounded-full bg-accent" />
                        )}
                        <span className="ml-auto text-[10px] text-ink-faint shrink-0 num">
                          {fmtRelativeTime(note.createdAt)}
                        </span>
                      </div>
                      <div
                        className={`mt-1 text-xs leading-snug truncate ${
                          note.read ? "text-ink-soft" : "text-ink font-medium"
                        }`}
                      >
                        {note.title}
                      </div>
                      {note.body && (
                        <div className="mt-0.5 text-[11px] text-ink-faint line-clamp-2 break-words">
                          {note.body}
                        </div>
                      )}
                    </div>
                  </button>
                );
              })
            )}
          </div>

          <button
            type="button"
            onClick={() => {
              navigate("/notifications");
              setOpen(false);
            }}
            className="w-full py-2.5 text-[11.5px] text-ink-soft hover:text-ink hover:bg-mantle border-t border-line"
          >
            打开完整收件箱
          </button>
        </div>
      )}
    </div>
  );
};
