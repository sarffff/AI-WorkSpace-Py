import { Bot } from "lucide-react";
import type { ResumableRun } from "@/shared/types/api.types";

/**
 * "这个会话有一次没跑完的执行"提示条。
 *
 * 与审批卡片、澄清卡片是三回事，所以不共用组件：那两张在等人**做决定**（批不批、
 * 答什么），这张只是等人点一下"要不要接回去"——不点也没有任何东西卡着。把它们
 * 合并成一个"待处理中断"会让文案不得不同时解释三种语义，而少解释的那一种会被
 * 用户当成故障。
 */
export function ResumableStrip({
  runs,
  onContinue,
}: {
  runs: ResumableRun[];
  onContinue: (run: ResumableRun) => void;
}) {
  if (runs.length === 0) return null;

  return (
    <div className="flex items-start gap-4 max-w-3xl">
      <div className="w-8 h-8 rounded-xl bg-[#282724] dark:bg-[#2e2d2a] flex items-center justify-center text-amber-500 shadow-sm">
        <Bot className="w-4 h-4" />
      </div>
      <div className="min-w-0 flex-1 rounded-2xl border border-amber-500/30 bg-amber-500/5 px-4 py-3">
        <p className="text-sm text-[#3d3929] dark:text-[#c8c5ba]">
          {runs.length === 1
            ? "这个会话有一次没跑完的执行。"
            : `这个会话有 ${runs.length} 次没跑完的执行。`}
        </p>
        <p className="mt-1 text-xs text-[#7c7a70] dark:text-[#8a8880]">
          连接断了，没有人做错什么。接着跑会带上当时已经检索到的东西；重问一遍则要从头再查。
        </p>
        <ul className="mt-3 flex flex-wrap gap-2">
          {runs.map((run) => (
            <li key={run.runId}>
              <button
                // 用 runId 而不是数组下标当 aria 标签的一部分：两条以上时用户要
                // 能分辨点的是哪一次，而下标会在列表变化时指到别的 run 上。
                onClick={() => onContinue(run)}
                aria-label={
                  run.round > 0 ? `接着跑第 ${run.round} 轮之后断掉的执行` : "接着跑这次断掉的执行"
                }
                className="px-3 py-1.5 rounded-xl bg-[#da7756] hover:bg-[#c96a4b] text-white text-xs font-medium transition-colors"
              >
                {run.round > 0 ? `接着跑（断在第 ${run.round} 轮）` : "接着跑"}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
