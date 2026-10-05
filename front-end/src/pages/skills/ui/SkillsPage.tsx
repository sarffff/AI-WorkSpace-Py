import React from "react";
import { PageHeader } from "@/shared/ui/PageHeader";
import { SkillPanel } from "../components/SkillPanel";

/**
 * 作业指导（SOP）是一个顶层模块，不是设置里的一格。
 *
 * 理由和后端一致：SOP 是**组织资产**。它决定 Agent 怎么办全工作区的单——
 * 内置那份 `refund-playbook` 说的是"退多少钱要有依据、什么时候必须转人工"，
 * 这属于流程，不属于个人偏好。
 *
 * 机制上这里显示的是索引，正文按需加载：Agent 每轮只看到"名字 + 一句用途"，
 * 判断相关时才 `load_skill` 取全文。所以**描述写不好等于这份 SOP 不存在**——
 * 那句话是模型选它的唯一依据，界面上要把它标成必填的理由也在这里。
 */
export const SkillsPage: React.FC = () => (
  <div className="page-shell">
    <div className="max-w-[900px] mx-auto flex flex-col gap-5">
      <PageHeader
        description="Agent 办单前看到的清单。命中某一份时它自己取全文并按规程办；同名的工作区版本会盖掉内置版本。"
      />
      <p className="text-[11.5px] text-ink-faint -mt-2 leading-relaxed">
        「必备材料」那一列目前只是给人看的约定：Agent 会读到它（在正文里），
        但**没有**"材料不齐就自动挂人工"那条硬规则——图里还没有这个判据。
      </p>
      <SkillPanel />
    </div>
  </div>
);
