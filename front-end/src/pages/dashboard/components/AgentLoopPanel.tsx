import React from "react";
import {
  GitBranch,
  Search,
  BrainCircuit,
  ShieldCheck,
} from "lucide-react";

export const AgentLoopPanel: React.FC<{
  serverStatus: string;
  selectedModel: string;
  sessionCount: number;
  onChat: () => void;
  onKnowledge: () => void;
  onTraces: () => void;
}> = ({
  serverStatus,
  selectedModel,
  sessionCount,
  onChat,
  onKnowledge,
  onTraces,
}) => {
  const online = serverStatus === "online";
  const stages = [
    {
      label: "路由",
      hint: "理解意图",
      icon: <GitBranch className="w-3.5 h-3.5" />,
      action: onChat,
    },
    {
      label: "检索",
      hint: "混合召回",
      icon: <Search className="w-3.5 h-3.5" />,
      action: onKnowledge,
    },
    {
      label: "推理",
      hint: selectedModel,
      icon: <BrainCircuit className="w-3.5 h-3.5" />,
      action: onChat,
    },
    {
      label: "护栏",
      hint: "输出校验",
      icon: <ShieldCheck className="w-3.5 h-3.5" />,
      action: onTraces,
    },
  ];

  return (
    <section className="card-surface rounded-2xl p-4 sm:p-5 anim-fade-up stagger-2 overflow-hidden relative">
      <div className="absolute inset-y-0 right-0 w-1/3 pointer-events-none opacity-40 bg-[radial-gradient(circle_at_70%_20%,rgba(218,119,86,0.14),transparent_65%)]" />
      <div className="relative flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2 mb-1.5">
            <span
              className={`relative flex w-2 h-2 ${online ? "pulse-ring" : ""}`}
            >
              <span
                className={`relative inline-flex rounded-full h-2 w-2 ${online ? "bg-emerald-500" : "bg-amber-500"}`}
              />
            </span>
            <span className="label-eyebrow">Agent 循环</span>
            <span className="chip text-[9px] px-1.5 py-0.5">
              {online ? "可运行" : "等待服务"}
            </span>
          </div>
          <h2 className="font-display text-base font-semibold text-[#1f1e1d] dark:text-[#edece8]">
            一次回答，四个可观测阶段
          </h2>
          <p className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] mt-1 max-w-md leading-relaxed">
            从意图路由到检索、模型推理，再到安全校验。每一步都能单独跳转查看证据。
          </p>
        </div>

        <div className="flex items-center gap-2.5 sm:gap-3 overflow-x-auto pb-1 lg:pb-0 lg:min-w-[31rem]">
          {stages.map((stage, index) => (
            <React.Fragment key={stage.label}>
              <button
                type="button"
                onClick={stage.action}
                className="group flex min-w-[5.5rem] flex-col items-center gap-1.5 rounded-xl px-2 py-2 transition-colors hover:bg-[#f3f0e6] dark:hover:bg-[#262522]"
              >
                <span className="flex h-8 w-8 items-center justify-center rounded-xl border border-[#e3dfd5] bg-[#fbf9f5] text-[#da7756] shadow-sm transition-transform group-hover:-translate-y-0.5 dark:border-[#33312d] dark:bg-[#1e1d1b]">
                  {stage.icon}
                </span>
                <span className="text-[11px] font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                  {stage.label}
                </span>
                <span
                  className="max-w-[6.5rem] truncate text-[9px] text-[#918d83]"
                  title={stage.hint}
                >
                  {stage.hint}
                </span>
              </button>
              {index < stages.length - 1 && (
                <span className="h-px w-4 shrink-0 bg-[#dcd7cb] dark:bg-[#33312d]" />
              )}
            </React.Fragment>
          ))}
        </div>

        <div className="hidden shrink-0 border-l border-[#e6e2d8] pl-4 text-right dark:border-[#2a2926] lg:block">
          <div className="label-eyebrow mb-1">当前上下文</div>
          <div className="font-mono text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
            {sessionCount}
          </div>
          <div className="text-[10px] text-[#918d83]">个会话</div>
        </div>
      </div>
    </section>
  );
};
