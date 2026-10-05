import React from "react";
import { PauseOctagon, PlayCircle } from "lucide-react";

export interface ShiftCell {
  key: string;
  label: string;
  value: string;
  hint?: string;
  /** 越线的那一格：底色换成告警色 */
  alarm?: boolean;
  onClick?: () => void;
}

/**
 * 值班条：坐席每天打开界面先看的那一排数。
 *
 * 每一格都必须能回答"然后我该做什么"，所以它们全是**可点的过滤器**，
 * 不是装饰性的统计卡。只有暂停/恢复那一格是动作而不是筛选。
 */
export const ShiftBoard: React.FC<{
  cells: ShiftCell[];
  paused: boolean;
  pauseReason: string | null;
}> = ({ cells, paused, pauseReason }) => (
  <div className="flex flex-col gap-2">
    <div className="shift-board">
      {cells.map((cell) => {
        const asButton = Boolean(cell.onClick);
        return (
          <button
            key={cell.key}
            type="button"
            onClick={cell.onClick}
            disabled={!asButton}
            className={`shift-cell text-left ${asButton ? "cursor-pointer hover:bg-overlay" : "cursor-default"}`}
            data-alarm={cell.alarm ? "true" : "false"}
            title={cell.hint}
          >
            <span className="shift-key">{cell.label}</span>
            <span className="shift-value num">{cell.value}</span>
            {cell.hint && <span className="text-[10.5px] text-ink-faint">{cell.hint}</span>}
          </button>
        );
      })}
    </div>

    <div
      className="flex items-center gap-2 px-3 py-2 rounded-xl text-[12px]"
      style={
        paused
          ? {
              background: "var(--c-bad-faint)",
              border: "1px solid color-mix(in srgb, var(--c-bad) 35%, transparent)",
              color: "var(--c-bad)",
            }
          : {
              background: "var(--c-done-faint)",
              border: "1px solid color-mix(in srgb, var(--c-done) 30%, transparent)",
              color: "var(--c-done)",
            }
      }
    >
      {paused ? (
        <PauseOctagon className="w-4 h-4 shrink-0" />
      ) : (
        <PlayCircle className="w-4 h-4 shrink-0" />
      )}
      <span className="font-semibold">
        {paused ? "全线暂停中：Agent 不会执行任何写操作" : "运行中：低风险工单会自动办完"}
      </span>
      {paused && pauseReason && (
        <span className="text-ink-soft truncate max-w-[52ch]">· {pauseReason}</span>
      )}
    </div>
  </div>
);
