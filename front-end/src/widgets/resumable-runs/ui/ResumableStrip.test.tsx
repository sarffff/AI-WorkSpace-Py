import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ResumableRun } from "@/shared/types/api.types";
import { ResumableStrip } from "./ResumableStrip";

/**
 * "没跑完的执行"提示条。测的四件事都对应一个会真实发生的错：
 *
 * - 空列表渲染出一条空条 → 用户看到一个没有内容的警告框。
 * - 多条时点第二条却接回第一条 → 这仓库已经犯过一次（通知只按 chatId 取
 *   第一条挂起项）。列表与回调之间必须按 runId 对应，不能按下标。
 * - `round=0` 显示"断在第 0 轮" → 那句话是错的，第 0 轮意味着断在第一次模型
 *   调用之前，等于什么都没跑，文案却像跑了很多轮。
 * - 用 div onClick 当按钮 → 键盘用户看得到提示却按不了（同仓库另一处已知问题）。
 */
describe("ResumableStrip", () => {
  // vitest.config.ts 没开 globals，所以没有自动 cleanup。少了这一句，上一个用例
  // 渲染的按钮会留在 DOM 里，getAllByRole("button") 的条数就不可信了。
  afterEach(cleanup);

  const run = (over: Partial<ResumableRun>): ResumableRun => ({
    runId: "run-1",
    chatId: "chat-1",
    round: 3,
    ...over,
  });

  it("没有可接续的执行时什么都不渲染", () => {
    const { container } = render(<ResumableStrip runs={[]} onContinue={vi.fn()} />);
    expect(container.innerHTML).toBe("");
  });

  it("多条时点哪一条就接回那一条", () => {
    const onContinue = vi.fn();
    const first = run({ runId: "run-A", round: 2 });
    const second = run({ runId: "run-B", round: 5 });
    render(<ResumableStrip runs={[first, second]} onContinue={onContinue} />);

    fireEvent.click(screen.getByText("接着跑（断在第 5 轮）"));
    expect(onContinue).toHaveBeenCalledWith(second);
    expect(onContinue).not.toHaveBeenCalledWith(first);
  });

  it("条数进文案", () => {
    render(
      <ResumableStrip
        runs={[run({ runId: "run-A" }), run({ runId: "run-B" })]}
        onContinue={vi.fn()}
      />,
    );
    expect(screen.getByText("这个会话有 2 次没跑完的执行。")).toBeTruthy();
  });

  it("断在第 0 轮时不说“第 0 轮”", () => {
    render(
      <ResumableStrip runs={[run({ round: 0 })]} onContinue={vi.fn()} />,
    );
    expect(screen.queryByText(/第 0 轮/)).toBeNull();
    expect(screen.getByText("接着跑")).toBeTruthy();
  });

  it("是可聚焦的按钮，不是一块带 onClick 的 div", () => {
    render(<ResumableStrip runs={[run({})]} onContinue={vi.fn()} />);
    const button = screen.getByRole("button", { name: "接着跑第 3 轮之后断掉的执行" });
    fireEvent.click(button);
    expect(button.tagName).toBe("BUTTON");
  });
});
