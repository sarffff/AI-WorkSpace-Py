import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Activity, BellOff, CheckCheck, Inbox, Loader2, ShieldAlert, Users } from "lucide-react";
import { apiClient } from "@/shared/api/client";
import { fmtDateTime, fmtRelativeTime } from "@/shared/lib/format";
import { EmptyState } from "@/shared/ui/EmptyState";
import { PageHeader } from "@/shared/ui/PageHeader";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import type { AppNotification } from "@/shared/types/api.types";

const KIND_META: Record<string, { label: string; icon: typeof BellOff; varName: string }> = {
  approval_required: { label: "等你批", icon: ShieldAlert, varName: "--c-hold" },
  ticket_handoff: { label: "转人工", icon: Users, varName: "--c-bad" },
  health_alert: { label: "健康告警", icon: Activity, varName: "--c-bad" },
};

export const NotificationsPage: React.FC = () => {
  const navigate = useNavigate();
  const toast = useToast();
  const [items, setItems] = useState<AppNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const [onlyUnread, setOnlyUnread] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiClient.getNotifications(onlyUnread, 100, 0);
      setItems(res.notifications);
      setUnread(res.unreadCount);
    } catch (e) {
      toast.error(toastMessageFrom(e, "通知读取失败"));
    } finally {
      setLoading(false);
    }
  }, [onlyUnread, toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const readOne = async (note: AppNotification) => {
    if (!note.read) {
      setItems((prev) => prev.map((n) => (n.id === note.id ? { ...n, read: true } : n)));
      setUnread((count) => Math.max(0, count - 1));
      apiClient.markNotificationRead(note.id).catch(() => undefined);
    }
    if (note.ticketId) navigate(`/tickets/${note.ticketId}`);
    else if (note.kind === "health_alert") navigate("/metrics");
  };

  return (
    <div className="page-shell">
      <div className="max-w-[780px] mx-auto flex flex-col gap-4">
        <PageHeader
          description="挂起等人批、SLA 到点转人工、线上越阈值。点一条就跳到那件事的现场。"
          actions={
            <>
              <div className="seg-switch" role="group" aria-label="筛选">
                <button
                  type="button"
                  data-active={!onlyUnread ? "true" : "false"}
                  onClick={() => setOnlyUnread(false)}
                >
                  全部
                </button>
                <button
                  type="button"
                  data-active={onlyUnread ? "true" : "false"}
                  onClick={() => setOnlyUnread(true)}
                >
                  未读 {unread > 0 ? `(${unread})` : ""}
                </button>
              </div>
              <button
                type="button"
                className="btn-quiet"
                disabled={unread === 0}
                onClick={() => {
                  setItems((prev) => prev.map((n) => ({ ...n, read: true })));
                  setUnread(0);
                  apiClient
                    .markAllNotificationsRead()
                    .catch((e) => {
                      toast.error(toastMessageFrom(e, "标记已读失败"));
                      void load();
                    });
                }}
              >
                <CheckCheck className="w-3.5 h-3.5" />
                全部已读
              </button>
            </>
          }
        />

        <div className="card-surface rounded-2xl overflow-hidden">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-14 text-ink-soft text-sm">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在读取…
            </div>
          ) : items.length === 0 ? (
            <div className="py-14">
              <EmptyState
                icon={<Inbox className="w-7 h-7 text-accent" />}
                title={onlyUnread ? "没有未读" : "还没有通知"}
                description="没有人被叫来看某件事，通常说明 Agent 自己把它办完了。"
              />
            </div>
          ) : (
            items.map((note) => {
              const meta = KIND_META[note.kind] ?? {
                label: "通知",
                icon: BellOff,
                varName: "--c-ink-soft",
              };
              const Icon = meta.icon;
              return (
                <button
                  key={note.id}
                  type="button"
                  onClick={() => void readOne(note)}
                  className="ledger-row w-full text-left px-4 py-3 gap-3 items-start"
                  data-selected={note.read ? "false" : "true"}
                >
                  <Icon
                    className="w-4 h-4 mt-0.5 shrink-0"
                    style={{ color: `var(${meta.varName})` }}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="chip text-[9px]">{meta.label}</span>
                      <span className="text-[13px] font-medium text-ink truncate min-w-0">
                        {note.title}
                      </span>
                      <span className="ml-auto text-[10.5px] text-ink-faint shrink-0 num">
                        {fmtRelativeTime(note.createdAt)}
                      </span>
                    </div>
                    {note.body && (
                      <p className="text-[11.5px] text-ink-soft mt-1 leading-relaxed break-words">
                        {note.body}
                      </p>
                    )}
                    <div className="text-[10px] text-ink-faint mt-1 num">
                      {fmtDateTime(note.createdAt)}
                      {note.ticketId ? ` · 工单 ${note.ticketId.slice(0, 8)}` : ""}
                    </div>
                  </div>
                </button>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};
