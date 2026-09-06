import React, { useState } from "react";
import { ChevronDown, ChevronRight, ListChecks } from "lucide-react";
import { toolLabel } from "@/shared/lib/format";
import type { PlanStep } from "@/shared/types/api.types";

/**
 * 事前规划卡片：模型动手之前先列出的步骤。
 *
 * **这不是进度条，是路线图。** 步骤上没有勾、没有"进行中"，因为后端不跟踪进度：
 * 一轮里可能并行调三个工具，也可能一轮什么都没做完，没有任何可靠信号说明"这一步
 * 完成了"。按轮次推游标是个看起来精确的假数字（理由写在后端 services/planner.py
 * 的模块文档里）。给它加上勾会让人以为那是实测的执行状态——那比不显示更糟。
 *
 * 计划对模型本身也只是路线图而不是命令（注入时的措辞见 planner._PLAN_NOTICE），
 * 所以实际执行顺序和这里不一致是**正常的**，不是 bug。要核对"计划了却没照做"
 * 得看下面的工具轨迹，或者评估里的 planAdherence 指标。
 *
 * 只在计划非空时渲染：模型判断"直接答就行"是正确输出，那时后端根本不发这个事件。
 *
 * 默认展开而不是折叠（和 ToolTrace 相反）：工具轨迹是事后排查用的，计划是**事前**
 * 的，它的用处恰恰在于让人在答案还没出来的时候就知道模型打算干什么。
 */

interface PlanCardProps {
  steps: PlanStep[];
}

export const PlanCard: React.FC<PlanCardProps> = ({ steps }) => {
  const [open, setOpen] = useState(true);

  if (!steps?.length) return null;

  return (
    <div className="my-3 rounded-xl border border-[#e3dfd5] dark:border-[#2e2d2a] bg-[#faf9f5] dark:bg-[#1f1e1c] overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="w-full flex items-center gap-2 px-3.5 py-2.5 text-left hover:bg-[#e3dfd5]/40 dark:hover:bg-[#2e2d2a]/60"
      >
        {open ? (
          <ChevronDown className="w-3.5 h-3.5 shrink-0 text-[#a19f96]" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 shrink-0 text-[#a19f96]" />
        )}
        <ListChecks className="w-4 h-4 shrink-0 text-[#6e6b63] dark:text-[#a19f96]" />
        <span className="text-xs font-medium text-[#3d3929] dark:text-[#e8e6dc]">
          执行计划
        </span>
        <span className="text-[11px] text-[#a19f96]">{steps.length} 步</span>
      </button>

      {open ? (
        <ol className="px-3.5 pb-3 space-y-1.5">
          {steps.map((step, index) => (
            <li
              key={`${index}-${step.goal}`}
              className="flex gap-2.5 text-xs leading-relaxed"
            >
              <span className="shrink-0 w-4 text-right text-[#a19f96] tabular-nums">
                {index + 1}
              </span>
              <span className="min-w-0 flex-1 text-[#3d3929] dark:text-[#e8e6dc]">
                {step.goal}
                {/*
                  工具名是提示而不是保证：模型可以不照计划调。空 tool 是**合法的**
                  （纯推理步骤，见 PlanStep 的注释），那时不渲染徽章而不是显示
                  一个"无工具"标签——后者会让人以为缺了什么。
                */}
                {step.tool?.trim() ? (
                  <span className="ml-1.5 inline-block px-1.5 py-0.5 rounded text-[10px] align-middle bg-[#e3dfd5]/70 dark:bg-[#2e2d2a] text-[#6e6b63] dark:text-[#a19f96]">
                    {toolLabel(step.tool)}
                  </span>
                ) : null}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
};
