import React from "react";
import { PageHeader } from "@/shared/ui/PageHeader";
import { UsagePanel } from "@/widgets/usage-panel/ui/UsagePanel";
import { AgentMetricsPanel } from "@/widgets/agent-metrics";

/**
 * 运营指标页：把两块本来写好、却没挂到任何页面的面板摆上台面。
 *
 * - **用量与成本**（UsagePanel）：钱花在哪个环节、按模型分桶、缓存命中、回答满意度。
 * - **Agent 执行**（AgentMetricsPanel）：`/metrics/agents` 的唯一消费者，重心是委派 vs
 *   未委派的代价对比（多花几倍钱、慢几倍）。委派默认关，关着时面板显示"未开启"，
 *   而不是画一屏全零——那两件事看起来一样、含义相反。
 *
 * 与工作台分工：工作台是"此刻该关心什么"的概览（待办 + 4 张小卡 + 最近运行），这里是
 * 运营者的成本/委派深看。两块面板各自带时间窗口选择器、各自取数，互不耦合。
 */
export const MetricsPage: React.FC = () => {
  return (
    <div className="page-shell app-atmosphere transition-colors duration-200">
      <div className="relative z-10 space-y-5 max-w-5xl">
        <PageHeader
          eyebrow="Metrics"
          title="运营指标"
          description="用量花在哪个环节、委派到底值不值——把一段时间的真实流量聚起来看，不是单次回答能看出来的。"
        />
        <UsagePanel />
        <AgentMetricsPanel />
      </div>
    </div>
  );
};
