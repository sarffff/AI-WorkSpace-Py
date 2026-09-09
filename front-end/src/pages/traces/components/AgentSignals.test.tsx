import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { AgentSignals, partitionAttributes } from "./AgentSignals";

/**
 * 回合信号面板。
 *
 * 要钉住的核心性质是**不认识的键不许消失**。后端 `turn.set()` 加键是很随意的动作，
 * 这一版就加了 `skills_loaded`、`budget_reclaimed` 两个；如果这里只渲染白名单，
 * 下一个新键会静默沉掉——界面看着整齐，实际在骗人。所以未识别的键必须原样回到
 * `rest` 里由上层打印。
 *
 * 第二个性质是**空数组不等于键不存在**。`skills_loaded: []` 是"索引注入了、模型
 * 一个都没调"，正是 AGENT_STATUS 里点名要盯的失效；键根本不存在只是这个 span 与
 * skill 无关。两者渲染必须不同。
 */
describe("partitionAttributes", () => {
  it("不认识的键原样留在 rest 里，不吞掉", () => {
    const { signals, rest } = partitionAttributes({
      rounds: 3,
      some_future_key: "后端明天加的",
      another_one: 42,
    });
    expect(signals.map((s) => s.key)).toEqual(["rounds"]);
    // 这两条是这个文件存在的理由
    expect(rest).toEqual({ some_future_key: "后端明天加的", another_one: 42 });
  });

  it("skills_loaded 空数组要报警，且和键不存在区分开", () => {
    const empty = partitionAttributes({ skills_loaded: [] });
    expect(empty.signals).toHaveLength(1);
    expect(empty.signals[0].warn).toBe(true);
    expect(empty.signals[0].value).toBe("无");
    expect(empty.signals[0].hint).toBeTruthy();

    // 键不存在：这个 span 跟 skill 无关，不该出现任何一行
    const absent = partitionAttributes({ rounds: 2 });
    expect(absent.signals.map((s) => s.key)).not.toContain("skills_loaded");
  });

  it("skills_loaded 有值时不报警，名字连起来显示", () => {
    const { signals } = partitionAttributes({
      skills_loaded: ["expense-review", "onboarding"],
    });
    expect(signals[0].warn).toBe(false);
    expect(signals[0].value).toBe("expense-review、onboarding");
    expect(signals[0].hint).toBeUndefined();
  });

  it("false / 0 / 空数组都是有意义的值，不能当缺数据丢掉", () => {
    const { signals } = partitionAttributes({
      cache_hit: false,
      budget_reclaimed: 0,
      vision_skipped: [],
    });
    const keys = signals.map((s) => s.key);
    expect(keys).toContain("cache_hit");
    expect(keys).toContain("budget_reclaimed");
    // 0 字符要显示成"0 字符"而不是被跳过
    expect(signals.find((s) => s.key === "budget_reclaimed")?.value).toBe(
      "0 字符",
    );
    expect(signals.find((s) => s.key === "cache_hit")?.value).toBe("未命中");
  });

  it("null / undefined 当作没这回事，直接不出现", () => {
    const { signals, rest } = partitionAttributes({
      rounds: null,
      plan_steps: undefined,
    });
    expect(signals).toHaveLength(0);
    // 也不该掉进 rest 去被原样打印
    expect(rest).toEqual({});
  });

  it("repeated_blocked 大于 0 才报警", () => {
    expect(
      partitionAttributes({ repeated_blocked: 0 }).signals[0].warn,
    ).toBe(false);
    expect(
      partitionAttributes({ repeated_blocked: 2 }).signals[0].warn,
    ).toBe(true);
  });

  it("中断原因翻译成中文，且一律算警示", () => {
    const { signals } = partitionAttributes({
      interrupted: "tool_approval",
    });
    expect(signals[0].value).toBe("等待工具审批");
    expect(signals[0].warn).toBe(true);
  });

  it("让路给 skill 与相关度都抬成命名行", () => {
    const { signals, rest } = partitionAttributes({
      skill_preempted: "expense-review",
      skill_similarity: 0.72,
    });
    const keys = signals.map((x) => x.key);
    expect(keys).toContain("skill_preempted");
    expect(keys).toContain("skill_similarity");
    // 这两个键是 2026-09-06 才加的；漏掉映射时它们会掉进 rest 被原样打印
    expect(rest).toEqual({});
  });

  it("让了路却还是没加载才是真失效", () => {
    // 两个键一起出现时，读的人要能分清是"预检索挡住了"还是"让了路也没用"
    const { signals } = partitionAttributes({
      skills_loaded: [],
      skill_preempted: "expense-review",
    });
    const loaded = signals.find((x) => x.key === "skills_loaded");
    expect(loaded?.warn).toBe(true);
    expect(signals.find((x) => x.key === "skill_preempted")).toBeTruthy();
  });

  it("展示顺序固定，不跟着后端 JSON 的键序变", () => {
    const a = partitionAttributes({ rounds: 1, skills_loaded: ["x"] });
    const b = partitionAttributes({ skills_loaded: ["x"], rounds: 1 });
    expect(a.signals.map((s) => s.key)).toEqual(b.signals.map((s) => s.key));
    // skills_loaded 声明在 rounds 之前，所以它排在前面
    expect(a.signals[0].key).toBe("skills_loaded");
  });
});

describe("AgentSignals", () => {
  afterEach(cleanup);

  it("没有信号时整块不渲染", () => {
    const { container } = render(<AgentSignals signals={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("报警项把 hint 写在下面，不只藏在 title 里", () => {
    const { signals } = partitionAttributes({ skills_loaded: [] });
    render(<AgentSignals signals={signals} />);
    expect(screen.getByText("已加载 skill")).toBeTruthy();
    // hint 要肉眼可见：只挂 title 的话用户得去 hover 才知道这是个问题
    expect(screen.getByText(/索引注入了但一个都没调/)).toBeTruthy();
  });
});
