import React from "react";
import type { AgentMetrics } from "@/shared/types/api.types";
import { GitBranch, ShieldCheck } from "lucide-react";
import { fmtInt } from "@/shared/lib/format";
import { MODE_LABELS, fmtRate } from "./agentMeta";
import { Tag } from "./Tag";
import { Metric } from "./Metric";
import { RoleBars } from "./RoleBars";
import { Comparison } from "./Comparison";

export const Body: React.FC<{ metrics: AgentMetrics }> = ({ metrics }) => {
  const { totals, byRole, comparison } = metrics;

  // 快照关着的时候 agent_runs 一行都没有，所有数字都是 0。画一个全零面板会让人
  // 以为"跑了很多次但从来不委派"——那和"这个功能没开"看起来一样，含义完全相反。
  if (!metrics.enabled) {
    return (
      <div className="rounded-xl bg-[#faf9f5] dark:bg-[#191817] px-4 py-5 text-[11px] text-[#6e6b63] dark:text-[#a19f96] space-y-1.5">
        <div className="font-medium text-[#1f1e1d] dark:text-[#edece8]">
          执行记录未开启
        </div>
        <div>
          设置{" "}
          <code className="text-[10px]">AGENT_CHECKPOINT_ENABLED=true</code>{" "}
          后开始记录。这张表也是人工审批与中断恢复的前提。
        </div>
      </div>
    );
  }

  if (!totals.runs) {
    return (
      <div className="rounded-xl bg-[#faf9f5] dark:bg-[#191817] px-4 py-5 text-[11px] text-[#6e6b63] dark:text-[#a19f96]">
        这个时间窗口里还没有执行记录。
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-1.5 text-[10px]">
        <Tag
          icon={<GitBranch className="w-3 h-3" />}
          text={`委派 ${MODE_LABELS[metrics.delegationMode] ?? metrics.delegationMode}`}
        />
        <Tag
          icon={<ShieldCheck className="w-3 h-3" />}
          text={`审批 ${MODE_LABELS[metrics.approvalMode] ?? metrics.approvalMode}`}
        />
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <Metric label="总执行" value={fmtInt(totals.runs)} />
        <Metric
          label="委派率"
          value={fmtRate(totals.delegationRate)}
          hint={`${totals.delegatedRuns} / ${totals.runs} 次回答`}
        />
        <Metric label="平均轮次" value={(totals.avgRounds ?? 0).toFixed(1)} />
        <Metric
          label="审批打断"
          value={fmtInt(totals.interrupts)}
          hint={
            totals.waitingApproval
              ? `${totals.waitingApproval} 个待处理`
              : undefined
          }
          warn={totals.waitingApproval > 0}
        />
      </div>

      {totals.failedRuns > 0 && (
        <div className="text-[10px] text-red-500">
          有 {totals.failedRuns} 次执行失败
        </div>
      )}

      {byRole.length > 0 && <RoleBars rows={byRole} />}
      <Comparison rows={comparison} />
    </div>
  );
};
