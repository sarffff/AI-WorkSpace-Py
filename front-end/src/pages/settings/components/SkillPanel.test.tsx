import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { SkillPanel } from "./SkillPanel";

/**
 * 作业指导面板。
 *
 * 三条要钉的性质都属于"不说出来就会被误解"那一类，而不是渲染细节：
 *
 * 1. **被覆盖要标出来。** 同名时工作区那份盖掉内置。不标的话管理员看不出内置那份
 *    已经不生效，反过来也会以为自己写的那份没被采用——两种误解都会让人去改错的
 *    那一份。
 * 2. **后端没开开关要说清。** 否则管理员写完一份 SOP、看到它出现在列表里、
 *    然后发现 AI 完全不按它办事。
 * 3. **非管理员看不到编辑入口。** 一条 SOP 影响全工作区所有人的执行方式。
 */

vi.mock("@/shared/api/client", () => ({
  apiClient: { getSkills: vi.fn() },
}));

vi.mock("@/shared/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  toastMessageFrom: (e: unknown, fallback: string) =>
    e instanceof Error ? e.message : fallback,
}));

const { apiClient } = await import("@/shared/api/client");

const base = {
  builtin: [
    {
      name: "expense-review",
      description: "审核报销单",
      attachments: ["报销额度标准.md"],
      overridden: false,
    },
  ],
  workspace: [],
  enabled: true,
  canEdit: true,
};

describe("SkillPanel", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.mocked(apiClient.getSkills).mockReset();
  });

  it("列出内置指导及其附带文件", async () => {
    vi.mocked(apiClient.getSkills).mockResolvedValue(base);
    render(<SkillPanel />);

    await waitFor(() => expect(screen.getByText("expense-review")).toBeTruthy());
    expect(screen.getByText("审核报销单")).toBeTruthy();
    expect(screen.getByText("报销额度标准.md")).toBeTruthy();
  });

  it("被同名工作区指导覆盖时标出来", async () => {
    vi.mocked(apiClient.getSkills).mockResolvedValue({
      ...base,
      builtin: [{ ...base.builtin[0], overridden: true }],
      workspace: [
        {
          id: "s1",
          name: "expense-review",
          description: "本公司流程",
          instructions: "只看三项。",
          enabled: true,
          updatedAt: null,
        },
      ],
    });
    render(<SkillPanel />);

    await waitFor(() =>
      expect(screen.getByText(/已被本工作区同名指导覆盖/)).toBeTruthy(),
    );
  });

  it("后端没开开关时说清楚写了也不生效", async () => {
    vi.mocked(apiClient.getSkills).mockResolvedValue({
      ...base,
      enabled: false,
    });
    render(<SkillPanel />);

    await waitFor(() =>
      expect(screen.getByText(/服务端没有启用作业指导/)).toBeTruthy(),
    );
  });

  it("非管理员看不到新增入口", async () => {
    vi.mocked(apiClient.getSkills).mockResolvedValue({
      ...base,
      canEdit: false,
    });
    render(<SkillPanel />);

    await waitFor(() => expect(screen.getByText("expense-review")).toBeTruthy());
    expect(screen.queryByRole("button", { name: /新增/ })).toBeNull();
    expect(screen.getByText(/只有工作区管理员可以修改/)).toBeTruthy();
  });

  it("停用的工作区指导标出来", async () => {
    vi.mocked(apiClient.getSkills).mockResolvedValue({
      ...base,
      workspace: [
        {
          id: "s1",
          name: "report",
          description: "写季度报告",
          instructions: "按模板写。",
          enabled: false,
          updatedAt: null,
        },
      ],
    });
    render(<SkillPanel />);

    await waitFor(() => expect(screen.getByText("已停用")).toBeTruthy());
  });
});
