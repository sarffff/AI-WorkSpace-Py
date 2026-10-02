import React, { useEffect, useState } from "react";
import {
  AlertCircle,
  Boxes,
  Clock,
  Loader2,
  Layers,
  Wrench,
} from "lucide-react";
import { apiClient } from "@/shared/api/client";
import type {
  AgentRunDetail,
  CheckpointStateView,
} from "@/shared/types/api.types";
import { fmtRelativeTime, toolLabel } from "@/shared/lib/format";

/**
 * 只读回放（B4）：把一次执行的检查点目录渲染成时间线，点一格看那一轮当时的状态——
 * 模型手上有什么消息、想调哪些工具、预算还剩多少、哪些工具被熔断了。
 *
 * **只读、不重跑**：从第 N 轮真的重跑要 fork 新 run 并处理副作用工具的重放，是另一件
 * 事。这里回答的是"当时它在想什么、为什么走到这一步"，数据来自 agent_runs 的检查点
 * 快照（AGENT_CHECKPOINT_KEEP 只留最近若干份，太旧的格子点开会 404）。
 *
 * 自带 runId 作入口——调用方（轨迹页 ?run= / 对话里某条回答）手上有 runId 就能挂它。
 */
const STATUS_META: Record<string, { label: string; cls: string }> = {
  running: { label: "运行中", cls: "text-amber-600 dark:text-amber-400" },
  waiting_approval: { label: "待审批", cls: "text-[#da7756]" },
  waiting_input: { label: "待回答", cls: "text-[#da7756]" },
  interrupted: { label: "已中断", cls: "text-amber-600 dark:text-amber-400" },
  done: { label: "完成", cls: "text-emerald-600 dark:text-emerald-400" },
  failed: { label: "失败", cls: "text-rose-500" },
  abandoned: { label: "已废弃", cls: "text-[#918d83]" },
  cancelled: { label: "已取消", cls: "text-[#918d83]" },
};

const PHASE_LABEL: Record<string, string> = {
  pre_tools: "调用工具前",
  waiting_approval: "等待审批",
  post_tools: "工具执行后",
};

const ROLE_LABEL: Record<string, string> = {
  user: "用户",
  assistant: "助手",
  system: "系统",
  tool: "工具",
};

const statusMeta = (s: string) =>
  STATUS_META[s] ?? { label: s, cls: "text-[#6e6b63] dark:text-[#a19f96]" };

export const RunReplay: React.FC<{ runId: string }> = ({ runId }) => {
  const [run, setRun] = useState<AgentRunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedSeq, setSelectedSeq] = useState<number | null>(null);
  const [view, setView] = useState<CheckpointStateView | null>(null);
  const [viewLoading, setViewLoading] = useState(false);
  const [viewError, setViewError] = useState<string | null>(null);

  // 取执行详情：含检查点目录与子代理。默认选最后一个检查点（最近的状态）。
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    setSelectedSeq(null);
    setView(null);
    apiClient
      .getAgentRun(runId)
      .then((detail) => {
        if (!alive) return;
        setRun(detail);
        const last = detail.checkpoints[detail.checkpoints.length - 1];
        if (last) setSelectedSeq(last.seq);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "执行详情加载失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [runId]);

  // 取某个检查点当时的状态（已裁剪的只读视图）
  useEffect(() => {
    if (selectedSeq === null) {
      setView(null);
      return;
    }
    let alive = true;
    setViewLoading(true);
    setViewError(null);
    apiClient
      .getRunCheckpoint(runId, selectedSeq)
      .then((v) => {
        if (alive) setView(v);
      })
      .catch((e) => {
        if (alive) {
          setView(null);
          setViewError(e instanceof Error ? e.message : "该快照不可用");
        }
      })
      .finally(() => {
        if (alive) setViewLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [runId, selectedSeq]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-xs text-[#6e6b63] dark:text-[#a19f96] py-6">
        <Loader2 className="w-4 h-4 animate-spin" /> 正在加载执行详情...
      </div>
    );
  }
  if (error) {
    return (
      <div className="flex items-center gap-2 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs">
        <AlertCircle className="w-4 h-4 shrink-0" /> {error}
      </div>
    );
  }
  if (!run) return null;

  const meta = statusMeta(run.status);

  return (
    <div className="flex-1 min-h-0 flex flex-col gap-4">
      <div className="card-surface rounded-2xl p-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-2.5">
            <Boxes className="w-4 h-4 text-[#da7756]" />
            <span className={`text-sm font-semibold ${meta.cls}`}>
              {meta.label}
            </span>
            <span
              className="chip text-[9px]"
              style={{ fontFamily: "var(--font-mono)" }}
            >
              {run.runId.slice(0, 8)}
            </span>
          </div>
          {run.startedAt && (
            <span className="flex items-center gap-1 text-[11px] text-[#918d83]">
              <Clock className="w-3 h-3" /> {fmtRelativeTime(run.startedAt)}
            </span>
          )}
        </div>
        <div className="mt-3 flex items-center gap-1.5 flex-wrap">
          <span className="chip text-[9px]">轮次 {run.rounds}</span>
          <span className="chip text-[9px]">委派 {run.delegations}</span>
          <span className="chip text-[9px]">中断 {run.interrupts}</span>
          {run.model && <span className="chip text-[9px]">{run.model}</span>}
          {run.errorType && (
            <span className="chip text-[9px] text-rose-500">
              {run.errorType}
            </span>
          )}
        </div>
        {run.children.length > 0 && (
          <div className="mt-3 pt-3 border-t border-[#e6e2d8] dark:border-[#282724]">
            <div className="label-eyebrow mb-1.5">
              子代理 ({run.children.length})
            </div>
            <div className="flex flex-wrap gap-1.5">
              {run.children.map((child) => (
                <span key={child.runId} className="chip text-[9px]">
                  {child.agentRole || "子代理"} · {statusMeta(child.status).label}{" "}
                  · {child.rounds} 轮
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* BODY_MARKER */}
      <div className="flex-1 min-h-0 grid grid-cols-[240px_1fr] gap-4">
        <div className="card-surface rounded-2xl overflow-hidden flex flex-col">
          <div className="label-eyebrow px-4 pt-3 pb-2">
            检查点 · {run.checkpoints.length}
          </div>
          <div className="overflow-y-auto flex-1 divide-y divide-[#e6e2d8]/60 dark:divide-[#282724]/60">
            {run.checkpoints.length === 0 ? (
              <p className="px-4 py-8 text-center text-[11px] text-[#918d83] leading-relaxed">
                这次执行没有留下检查点（没开快照，或已清理）。
              </p>
            ) : (
              run.checkpoints.map((cp) => (
                <button
                  key={cp.seq}
                  onClick={() => setSelectedSeq(cp.seq)}
                  className={`w-full text-left px-4 py-3 text-xs transition-colors hover:bg-[#f3f0e6]/50 dark:hover:bg-[#22211e] ${
                    selectedSeq === cp.seq
                      ? "bg-[#eae6db] dark:bg-[#262522] border-l-[3px] border-[#da7756]"
                      : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-[#1f1e1d] dark:text-[#edece8]">
                      第 {cp.round} 轮
                    </span>
                    <span className="text-[10px] text-[#918d83]">#{cp.seq}</span>
                  </div>
                  <div className="mt-0.5 text-[11px] text-[#6e6b63] dark:text-[#a19f96]">
                    {PHASE_LABEL[cp.phase] ?? cp.phase}
                  </div>
                </button>
              ))
            )}
          </div>
        </div>

        {/* STATEVIEW_MARKER */}
        <div className="card-surface rounded-2xl overflow-y-auto">
          {selectedSeq === null ? (
            <div className="p-8 text-center text-xs text-[#918d83]">
              从左侧选择一个检查点，看那一轮当时的状态。
            </div>
          ) : viewLoading ? (
            <div className="p-8 flex items-center justify-center gap-2 text-xs text-[#6e6b63] dark:text-[#a19f96]">
              <Loader2 className="w-4 h-4 animate-spin" /> 加载快照...
            </div>
          ) : viewError ? (
            <div className="m-4 flex items-center gap-2 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs">
              <AlertCircle className="w-4 h-4 shrink-0" /> {viewError}
            </div>
          ) : view ? (
            <div className="p-4 space-y-4">
              <div className="flex flex-wrap gap-1.5">
                <span className="chip chip-accent text-[9px]">
                  第 {view.round} 轮
                </span>
                <span className="chip text-[9px]">
                  {PHASE_LABEL[view.phase] ?? view.phase}
                </span>
                <span className="chip text-[9px]">
                  预算余 {view.budgetRemaining}
                </span>
                {view.repeatBlocked > 0 && (
                  <span className="chip text-[9px]">
                    拦重复 {view.repeatBlocked}
                  </span>
                )}
                {view.delegationsUsed > 0 && (
                  <span className="chip text-[9px]">
                    已委派 {view.delegationsUsed}
                  </span>
                )}
              </div>

              {/* SECTIONS_MARKER */}
              {view.breakerTripped.length > 0 && (
                <div>
                  <div className="label-eyebrow mb-1.5 flex items-center gap-1.5">
                    <Wrench className="w-3 h-3" /> 已熔断工具
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {view.breakerTripped.map((t) => (
                      <span key={t} className="chip text-[9px] text-rose-500">
                        {toolLabel(t)}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {view.loadedSkills.length > 0 && (
                <div>
                  <div className="label-eyebrow mb-1.5">已加载作业指导</div>
                  <div className="flex flex-wrap gap-1.5">
                    {view.loadedSkills.map((s) => (
                      <span key={s} className="chip text-[9px]">
                        {s}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {view.plan.length > 0 && (
                <div>
                  <div className="label-eyebrow mb-1.5 flex items-center gap-1.5">
                    <Layers className="w-3 h-3" /> 事前规划
                  </div>
                  <ol className="space-y-1">
                    {view.plan.map((step, i) => (
                      <li
                        key={i}
                        className="text-[11px] text-[#1f1e1d] dark:text-[#edece8] flex gap-1.5"
                      >
                        <span className="text-[#918d83]">{i + 1}.</span>
                        <span className="flex-1">
                          {step.goal}
                          {step.tool && (
                            <span className="text-[#918d83]">
                              {" "}
                              · {toolLabel(step.tool)}
                            </span>
                          )}
                        </span>
                      </li>
                    ))}
                  </ol>
                </div>
              )}

              {/* MSG_MARKER */}
              {view.pendingCalls.length > 0 && (
                <div>
                  <div className="label-eyebrow mb-1.5 flex items-center gap-1.5">
                    <Wrench className="w-3 h-3" /> 待执行工具
                  </div>
                  <div className="space-y-1.5">
                    {view.pendingCalls.map((call, i) => (
                      <div
                        key={i}
                        className="rounded-lg border border-[#e6e2d8] dark:border-[#282724] bg-[#faf9f5] dark:bg-[#191817] px-2.5 py-1.5"
                      >
                        <span className="text-[11px] font-medium text-[#1f1e1d] dark:text-[#edece8]">
                          {toolLabel(call.name ?? undefined)}
                        </span>
                        {call.arguments && (
                          <pre
                            className="mt-1 text-[10px] text-[#6e6b63] dark:text-[#a19f96] whitespace-pre-wrap break-all"
                            style={{ fontFamily: "var(--font-mono)" }}
                          >
                            {call.arguments}
                          </pre>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div>
                <div className="label-eyebrow mb-1.5 flex items-center gap-1.5">
                  <Layers className="w-3 h-3" /> 消息（近 {view.messages.length} /
                  共 {view.messageCount}）
                </div>
                <div className="space-y-1.5">
                  {view.messages.map((m, i) => (
                    <div
                      key={i}
                      className="rounded-lg border border-[#e6e2d8] dark:border-[#282724] px-2.5 py-1.5"
                    >
                      <span className="chip text-[9px] mb-1">
                        {ROLE_LABEL[m.role] ?? m.role}
                      </span>
                      <p className="text-[11px] leading-relaxed text-[#1f1e1d] dark:text-[#edece8] whitespace-pre-wrap break-words">
                        {m.content}
                      </p>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};
