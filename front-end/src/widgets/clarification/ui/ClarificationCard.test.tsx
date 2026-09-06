import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ClarificationCard } from "./ClarificationCard";

/**
 * 澄清卡片。两种来源共用这一张卡：
 *
 * - 模型主动调了 `ask_user`（`adopted=false`）——问题只存在于中断里，卡片必须
 *   把它显示出来，否则用户不知道被问了什么。
 * - 框架从回答正文里收编（`adopted=true`）——那句问题就在上面那段回答的末尾，
 *   卡片再显示一遍等于问了两遍。
 *
 * 这个差别不是装饰：两种情况下"卡片该不该渲染 question"的答案相反，所以两边都要钉。
 */
describe("ClarificationCard", () => {
  // vitest.config.ts 没开 globals，所以没有自动 cleanup。少了这一句，上一个用例
  // 渲染的卡片会留在 DOM 里，getByRole("textbox") 报的是"找到多个"——看起来像
  // 组件渲染了两个输入框。
  afterEach(cleanup);

  const question = "请问您出差的城市属于哪个等级？";

  it("模型主动问时要把问题显示出来", () => {
    render(
      <ClarificationCard question={question} resumable onAnswer={vi.fn()} />,
    );
    expect(screen.getByText(question)).toBeTruthy();
    expect(screen.getByText("需要你补充一句")).toBeTruthy();
  });

  it("收编来的问题不再重复渲染一遍", () => {
    render(
      <ClarificationCard
        question={question}
        resumable
        adopted
        onAnswer={vi.fn()}
      />,
    );
    // 那句话就在上面的回答里，这里再来一遍就是问了两遍
    expect(screen.queryByText(question)).toBeNull();
    expect(screen.getByText("在这里回答就能接着上面那一步")).toBeTruthy();
  });

  it("空答案不能提交", () => {
    const onAnswer = vi.fn();
    render(
      <ClarificationCard question={question} resumable onAnswer={onAnswer} />,
    );
    const button = screen.getByRole("button", { name: /回答/ });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(button);
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("回答会去掉首尾空白再送出", () => {
    const onAnswer = vi.fn();
    render(
      <ClarificationCard question={question} resumable onAnswer={onAnswer} />,
    );
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "  二线城市  " },
    });
    fireEvent.click(screen.getByRole("button", { name: /回答/ }));
    expect(onAnswer).toHaveBeenCalledWith("二线城市");
  });

  it("Enter 直接发送，Shift+Enter 不发", () => {
    const onAnswer = vi.fn();
    render(
      <ClarificationCard question={question} resumable onAnswer={onAnswer} />,
    );
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "二线城市" } });

    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onAnswer).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    expect(onAnswer).toHaveBeenCalledWith("二线城市");
  });

  it("不可接续时要说清楚代价", () => {
    render(
      <ClarificationCard
        question={question}
        resumable={false}
        onAnswer={vi.fn()}
      />,
    );
    // 静默降级最糟：用户会以为模型只是又查了一遍
    expect(screen.getByText(/重新检索/)).toBeTruthy();
  });

  it("busy 时不能再送一次", () => {
    const onAnswer = vi.fn();
    render(
      <ClarificationCard
        question={question}
        resumable
        busy
        onAnswer={onAnswer}
      />,
    );
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    expect(onAnswer).not.toHaveBeenCalled();
  });
});
