import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { PlanCard } from "./PlanCard";

/**
 * 执行计划卡片。
 *
 * 要钉住的核心性质是**它不是进度条**：后端不跟踪步骤完成情况（没有可靠信号说明
 * "这一步完成了"，理由写在 services/planner.py），所以卡片上不能出现任何暗示
 * 执行状态的东西。加一个勾会让人以为那是实测的，那比不显示更糟。
 *
 * 空 tool 是合法输入而不是缺数据：纯推理的步骤（比较、汇总、下结论）本来就没有
 * 对应工具，提示词里明确要求这类步骤留空。
 */
describe("PlanCard", () => {
  // vitest.config.ts 没开 globals，没有自动 cleanup。少了这一句，上一个用例的
  // 卡片会留在 DOM 里，按文本查询报的是"找到多个"。
  afterEach(cleanup);

  const steps = [
    { goal: "查出差补贴标准", tool: "search_knowledge_base" },
    { goal: "比较两处规定的差异", tool: "" },
  ];

  it("按顺序渲染每一步，工具名显示成中文标签", () => {
    render(<PlanCard steps={steps} />);
    expect(screen.getByText("查出差补贴标准")).toBeTruthy();
    expect(screen.getByText("比较两处规定的差异")).toBeTruthy();
    // 走 TOOL_LABELS，不是原始英文名——审批与轨迹上同一套映射
    expect(screen.getByText("检索知识库")).toBeTruthy();
    expect(screen.getByText("2 步")).toBeTruthy();
  });

  it("tool 为空的步骤不渲染工具徽章", () => {
    render(<PlanCard steps={[{ goal: "汇总并下结论", tool: "" }]} />);
    expect(screen.getByText("汇总并下结论")).toBeTruthy();
    // 不能出现"无工具"之类的占位标签：空 tool 是正常的，标出来会让人以为缺了东西
    expect(screen.queryByText(/无工具|工具/)).toBeNull();
  });

  it("空计划不渲染任何东西", () => {
    // 后端在计划为空时根本不发事件，但组件自己也要挡一次——否则一张写着
    // 「0 步」的空卡片会出现在回答上方
    const { container } = render(<PlanCard steps={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("默认展开，能折叠起来", () => {
    render(<PlanCard steps={steps} />);
    expect(screen.getByText("查出差补贴标准")).toBeTruthy();
    fireEvent.click(screen.getByRole("button"));
    // 折叠后步骤不在 DOM 里，但标题和步数还在——那是判断"要不要展开"的依据
    expect(screen.queryByText("查出差补贴标准")).toBeNull();
    expect(screen.getByText("执行计划")).toBeTruthy();
    expect(screen.getByText("2 步")).toBeTruthy();
  });

  it("不渲染任何完成状态", () => {
    render(<PlanCard steps={steps} />);
    // 这条是防回归的：以后有人给步骤加勾、加"进行中"，得先想清楚进度是从哪来的。
    // 后端没有这个信号，前端自己按轮次推是个假数字。
    const text = screen.getByText("执行计划").closest("div")?.parentElement
      ?.textContent ?? "";
    expect(text).not.toMatch(/已完成|进行中|完成|✓/);
  });
});
