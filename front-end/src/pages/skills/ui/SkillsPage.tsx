import React from "react";
import { PageHeader } from "@/shared/ui/PageHeader";
import { SkillPanel } from "@/pages/settings/components/SkillPanel";

/**
 * 作业指导(SOP)升级为顶层模块。
 *
 * 原来埋在设置里。但 SOP 是企业资产——它影响全工作区所有人的执行方式，审核型 SOP
 * 声明的必备材料还直接决定 submit_review 的必填槽位与审核严格程度。这不是"个人偏好"
 * 那一档，值得一个自己的入口。这里复用现有 SkillPanel 组件，不重写。
 */
export const SkillsPage: React.FC = () => (
  <div className="page-shell app-atmosphere transition-colors duration-200">
    <div className="relative z-10 space-y-6 max-w-5xl">
      <PageHeader
        eyebrow="规程"
        title="作业指导"
        description="Agent 处理任务前先看这份清单，命中就按对应 SOP 执行。审核型 SOP 声明的必备材料，直接决定 submit_review 的必填槽位与审核严格程度。"
      />
      <SkillPanel />
    </div>
  </div>
);
