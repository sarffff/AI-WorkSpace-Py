import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  apiClient: { getSkills: vi.fn(), saveSkill: vi.fn(), deleteSkill: vi.fn() },
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
      requiredInputs: "出差城市, 发生日期, 费用类别, 发票或等效凭证",
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
          requiredInputs: "",
          version: 1,
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
          requiredInputs: "",
          version: 1,
          updatedAt: null,
        },
      ],
    });
    render(<SkillPanel />);

    await waitFor(() => expect(screen.getByText("已停用")).toBeTruthy());
  });

  it("显示必备材料与规程版本", async () => {
    vi.mocked(apiClient.getSkills).mockResolvedValue({
      ...base,
      workspace: [
        {
          id: "s1",
          name: "expense",
          description: "本公司报销流程",
          instructions: "按三步走。",
          enabled: true,
          requiredInputs: "金额, 凭证",
          version: 4,
          updatedAt: null,
        },
      ],
    });
    render(<SkillPanel />);

    // 必备材料是登记给复核的人看的，AI 侧看不到，所以列表里必须显示出来
    await waitFor(() => expect(screen.getByText(/金额, 凭证/)).toBeTruthy());
    // 版本号回答"这份正文动过几次"——表里只存最新一份，没这个号就看不出动过
    expect(screen.getByText(/第 4 版/)).toBeTruthy();
  });

  it("编辑已有指导时把必备材料带进草稿", async () => {
    // 这条钉的是覆盖陷阱：PUT 是整体覆盖，编辑时不回填原值的话，
    // 保存一次就把声明抹成空串——不报错，只是这份登记悄悄没了。
    vi.mocked(apiClient.getSkills).mockResolvedValue({
      ...base,
      workspace: [
        {
          id: "s1",
          name: "expense",
          description: "本公司报销流程",
          instructions: "按三步走。",
          enabled: true,
          requiredInputs: "金额, 凭证",
          version: 2,
          updatedAt: null,
        },
      ],
    });
    vi.mocked(apiClient.saveSkill).mockResolvedValue({
      id: "s1",
      name: "expense",
      description: "本公司报销流程",
      enabled: true,
      requiredInputs: "金额, 凭证",
      version: 2,
    });
    render(<SkillPanel />);

    await waitFor(() => expect(screen.getByTitle("编辑")).toBeTruthy());
    fireEvent.click(screen.getByTitle("编辑"));

    // 草稿里回填了原值，而不是空
    const field = await waitFor(() =>
      screen.getByDisplayValue("金额, 凭证"),
    );
    expect(field).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /保存/ }));
    await waitFor(() =>
      expect(vi.mocked(apiClient.saveSkill)).toHaveBeenCalledWith(
        expect.objectContaining({ requiredInputs: "金额, 凭证" }),
      ),
    );
  });
});
