import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Inbox, Loader2, Plus, RefreshCw, ShieldCheck, AlertTriangle } from "lucide-react";
import { apiClient, isConflictResponse } from "@/shared/api/client";
import { EmptyState } from "@/shared/ui/EmptyState";
import { PageHeader } from "@/shared/ui/PageHeader";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import type { TicketQueueResponse, TicketSummary } from "@/shared/types/api.types";
import { NewTicketForm } from "../components/NewTicketForm";
import { ShiftBoard, type ShiftCell } from "../components/ShiftBoard";
import { TicketRow } from "../components/TicketRow";

const PAGE = 100;

/**
 * 状态筛选。值原样交给后端（它按 status 精确过滤）。
 *
 * 这里刻意**没有**"在办"这种组合档：组合只能在前端对已加载的那一页做过滤，
 * 于是"等你批 3 张"和列表条数会来自两套口径，而顶栏的计数是全队列算的。
 * 与其给一个看起来精确、实际按页抖的数字，不如只给单档筛选。
 */
const STATUS_FILTERS: { key: string; label: string; status?: string; mine?: boolean }[] = [
  { key: "all", label: "全部" },
  { key: "awaiting_approval", label: "等你批", status: "awaiting_approval" },
  { key: "escalated", label: "已转人工", status: "escalated" },
  { key: "new", label: "待受理", status: "new" },
  { key: "resolved", label: "已解决", status: "resolved" },
  { key: "closed", label: "已关闭", status: "closed" },
  { key: "mine", label: "我受理的", mine: true },
];

export const TicketQueuePage: React.FC = () => {
  const navigate = useNavigate();
  const toast = useToast();

  const [filter, setFilter] = useState("all");
  const [board, setBoard] = useState<TicketQueueResponse | null>(null);
  const [rows, setRows] = useState<TicketSummary[]>([]);
  const [offset, setOffset] = useState(0);
  const [pendingCount, setPendingCount] = useState(0);
  const [governor, setGovernor] = useState<{ paused: boolean; reason: string | null }>({
    paused: false,
    reason: null,
  });
  const [loading, setLoading] = useState(true);
  const [appending, setAppending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [disabled, setDisabled] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);

  const active = useMemo(
    () => STATUS_FILTERS.find((item) => item.key === filter) ?? STATUS_FILTERS[0],
    [filter]
  );

  /**
   * 换筛选条件时整页重读，而不是把上一页带着走：翻页偏移量是按当前筛选算的，
   * 沿用旧 offset 会直接跳过一批工单，而界面上看不出少了几张。
   */
  useEffect(() => {
    setOffset(0);
  }, [filter]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [list, approvals, state] = await Promise.all([
        apiClient.listTickets({
          status: active.status,
          mine: active.mine,
          limit: PAGE,
          offset,
        }),
        apiClient.getPendingApprovals().catch(() => ({ count: 0, items: [] })),
        apiClient.governorState().catch(() => null),
      ]);

      if (isConflictResponse(list)) {
        setDisabled(list.message ?? "工单能力未开启");
        setRows([]);
        setBoard(null);
        return;
      }
      setDisabled(null);
      setBoard(list);
      setRows((previous) =>
        offset === 0 ? list.tickets : [...previous, ...list.tickets]
      );
      setPendingCount(approvals.count);
      if (state && !isConflictResponse(state)) {
        setGovernor({ paused: state.paused, reason: state.pauseReason });
      }
    } catch (e) {
      setError(toastMessageFrom(e, "队列加载失败"));
    } finally {
      setLoading(false);
      setAppending(false);
    }
  }, [active.mine, active.status, offset]);

  useEffect(() => {
    void load();
    // offset 变化时 load 已重建；这里只跟着 load 走，不额外设依赖
  }, [load]);

  const loadMore = async () => {
    setAppending(true);
    setOffset((value) => value + PAGE);
  };

  const cells: ShiftCell[] = [
    {
      key: "total",
      label: "全队列",
      value: String(board?.total ?? 0),
      hint: "当前筛选条件下的工单总数",
    },
    {
      key: "pending",
      label: "等你批",
      value: String(pendingCount),
      alarm: pendingCount > 0,
      hint: "资金类操作，等人点头才会执行",
      onClick: () => navigate("/approvals"),
    },
    {
      key: "overdue",
      label: "超期未闭",
      value: String(board?.overdueStillOpen ?? 0),
      alarm: (board?.overdueStillOpen ?? 0) > 0,
      hint: "SLA 到点仍没结束的工单",
    },
    {
      key: "reaped",
      label: "本轮回收",
      value: String(board?.reapedOverdue ?? 0),
      hint: "上次运行到点没跑完，已重新排队",
    },
  ];

  return (
    <div className="page-shell">
      <div className="max-w-[1180px] mx-auto flex flex-col gap-5">
        <PageHeader
          description="Agent 办到哪儿、什么在等你，都在这张台面上。低风险工单自动走完，资金类必须有人批。"
          actions={
            <>
              <button
                type="button"
                onClick={() => {
                  // 已经翻到第二页时先回到第一页：在 offset>0 上直接重读会把
                  // 第一页的数据追加到尾部，界面上出现重复的工单行
                  if (offset === 0) void load();
                  else setOffset(0);
                }}
                className="btn-quiet"
                aria-label="刷新队列"
              >
                <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
                刷新
              </button>
              <button
                type="button"
                onClick={() => setComposing((open) => !open)}
                className="btn-accent px-3.5 py-2 rounded-[10px] text-[12px] font-semibold"
                aria-expanded={composing}
              >
                <Plus className="w-3.5 h-3.5" />
                手动录入
              </button>
            </>
          }
        />

        <ShiftBoard
          cells={cells}
          paused={governor.paused}
          pauseReason={governor.reason}
        />

        {composing && (
          <NewTicketForm
            onSubmitted={(result) => {
              setComposing(false);
              if (!result.created) {
                toast.info(
                  `这张工单与 ${result.dedupedAgainst?.slice(0, 8) ?? "已有工单"} 重复，没有新建第二条`
                );
              } else {
                toast.success(`已受理，工单号 ${result.ticketId.slice(0, 8)}`);
              }
              navigate(`/tickets/${result.ticketId}`);
            }}
            onCancel={() => setComposing(false)}
          />
        )}

        <div className="flex items-center gap-1.5 flex-wrap">
          {STATUS_FILTERS.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setFilter(item.key)}
              className={`px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors ${
                filter === item.key
                  ? "bg-ink text-surface"
                  : "bg-surface border border-line text-ink-soft hover:border-line-strong"
              }`}
              aria-pressed={filter === item.key}
            >
              {item.label}
            </button>
          ))}
          <span className="ml-auto text-[11px] text-ink-faint num">
            本页 {rows.length} / 全队列 {board?.total ?? 0}
          </span>
        </div>

        {error && (
          <div className="flex items-center gap-2 px-3 py-2 rounded-xl text-[12px] text-state-bad" style={{ background: "var(--c-bad-faint)" }}>
            <AlertTriangle className="w-4 h-4 shrink-0" />
            {error}
            <button type="button" onClick={() => void load()} className="ml-auto btn-quiet py-1">
              重试
            </button>
          </div>
        )}

        {disabled && (
          <div className="card-surface rounded-2xl p-6 anim-fade-up">
            <EmptyState
              icon={<ShieldCheck className="w-7 h-7 text-accent" />}
              title="工单能力还没开"
              description={disabled}
            />
          </div>
        )}

        {!disabled && !error && (
          <div className="card-surface rounded-2xl overflow-hidden">
            {loading && rows.length === 0 ? (
              <div className="flex items-center justify-center gap-2 py-16 text-ink-soft text-sm">
                <Loader2 className="w-4 h-4 animate-spin" />
                正在读取队列…
              </div>
            ) : rows.length === 0 ? (
              <div className="py-14">
                <EmptyState
                  icon={<Inbox className="w-7 h-7 text-accent" />}
                  title={filter === "all" ? "一张工单都没有" : "这一档里没有工单"}
                  description="低风险工单会被 Agent 自己办完并回复客户；只有需要人批或办不成的才会留在这张台面上。"
                />
              </div>
            ) : (
              <div>
                {rows.map((ticket) => (
                  <TicketRow
                    key={ticket.id}
                    ticket={ticket}
                    onOpen={(id) => navigate(`/tickets/${id}`)}
                  />
                ))}
                <div className="px-4 py-3 flex items-center justify-between gap-3">
                  <span className="text-[11px] text-ink-faint num">
                    已显示 {rows.length} / {board?.total ?? 0}
                  </span>
                  {rows.length < (board?.total ?? 0) && (
                    <button
                      type="button"
                      onClick={() => void loadMore()}
                      disabled={appending}
                      className="btn-quiet py-1"
                    >
                      {appending ? "加载中…" : "加载更多"}
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
