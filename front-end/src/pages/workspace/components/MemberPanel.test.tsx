import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemberPanel } from "./MemberPanel";

/**
 * 工作区成员面板。
 *
 * 钉的四条都属于"点了才发现不行"那一类——它们不是渲染细节，而是把后端的拒绝
 * 提前变成禁用状态：
 *
 * 1. **最后一个管理员的降级按钮要禁掉。** 没有管理员的工作区不可恢复（改不了名、
 *    重置不了邀请码、再没人能提拔谁）。禁用判断用后端给的 `adminCount`，
 *    不自己数——那是把一条不变量抄到第二处。
 * 2. **自己那一行的移除按钮永久禁用。** "退出工作区"是另一个动作。
 * 3. **移除要二次确认。** 它让那个人立刻失去全部共享文档的访问，而这个界面上
 *    没有撤销。改角色可逆，所以不确认。
 * 4. **非管理员看不到任何操作入口。**
 */

vi.mock("@/shared/api/client", () => ({
  apiClient: {
    getWorkspace: vi.fn(),
    setMemberRole: vi.fn(),
    removeMember: vi.fn(),
  },
}));

vi.mock("@/shared/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  toastMessageFrom: (e: unknown, fallback: string) =>
    e instanceof Error ? e.message : fallback,
}));

const { apiClient } = await import("@/shared/api/client");

const team = {
  id: "w1",
  name: "团队空间",
  role: "admin" as const,
  isAdmin: true,
  memberCount: 2,
  adminCount: 1,
  inviteCode: "ABCD2345",
  members: [
    {
      id: "u1",
      name: "阿花",
      role: "admin" as const,
      email: "admin@example.com",
      isSelf: true,
    },
    {
      id: "u2",
      name: "小李",
      role: "user" as const,
      email: "member@example.com",
      isSelf: false,
    },
  ],
};

describe("MemberPanel", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.mocked(apiClient.getWorkspace).mockReset();
    vi.mocked(apiClient.setMemberRole).mockReset();
    vi.mocked(apiClient.removeMember).mockReset();
  });

  it("最后一个管理员的降级按钮禁掉，并说明原因", async () => {
    vi.mocked(apiClient.getWorkspace).mockResolvedValue(team);
    render(<MemberPanel />);
    await waitFor(() => expect(screen.getByText("阿花")).toBeTruthy());

    const demote = screen.getByText("降为成员") as HTMLButtonElement;
    expect(demote.disabled).toBe(true);
    // 光禁掉不说原因会让人以为界面坏了
    expect(demote.title).toContain("最后一个管理员");
  });

  it("有第二个管理员时降级按钮可用", async () => {
    vi.mocked(apiClient.getWorkspace).mockResolvedValue({
      ...team,
      adminCount: 2,
      members: [
        team.members[0],
        { ...team.members[1], role: "admin" as const },
      ],
    });
    render(<MemberPanel />);
    await waitFor(() => expect(screen.getByText("小李")).toBeTruthy());

    const buttons = screen.getAllByText("降为成员") as HTMLButtonElement[];
    expect(buttons).toHaveLength(2);
    expect(buttons.every((button) => button.disabled)).toBe(false);
  });

  it("自己那一行不能移除", async () => {
    vi.mocked(apiClient.getWorkspace).mockResolvedValue(team);
    render(<MemberPanel />);
    await waitFor(() => expect(screen.getByText("阿花")).toBeTruthy());

    const self = screen.getByLabelText("移除 阿花") as HTMLButtonElement;
    expect(self.disabled).toBe(true);
    expect(self.title).toContain("不能移除自己");

    const other = screen.getByLabelText("移除 小李") as HTMLButtonElement;
    expect(other.disabled).toBe(false);
  });

  it("移除要点两次，第一次只是确认", async () => {
    vi.mocked(apiClient.getWorkspace).mockResolvedValue(team);
    vi.mocked(apiClient.removeMember).mockResolvedValue({
      success: true,
      removed: { id: "u2", name: "小李" },
      workspace: { ...team, memberCount: 1, members: [team.members[0]] },
    });
    render(<MemberPanel />);
    await waitFor(() => expect(screen.getByText("小李")).toBeTruthy());

    fireEvent.click(screen.getByLabelText("移除 小李"));
    // 第一次点击不发请求
    expect(apiClient.removeMember).not.toHaveBeenCalled();
    expect(screen.getByText("确定移出？")).toBeTruthy();

    fireEvent.click(screen.getByText("移出"));
    await waitFor(() =>
      expect(apiClient.removeMember).toHaveBeenCalledWith("u2"),
    );
    // 响应里带着整份 workspace，列表直接用它更新，不再发一次 GET
    await waitFor(() => expect(screen.queryByText("小李")).toBeNull());
    expect(apiClient.getWorkspace).toHaveBeenCalledTimes(1);
  });

  it("确认可以取消", async () => {
    vi.mocked(apiClient.getWorkspace).mockResolvedValue(team);
    render(<MemberPanel />);
    await waitFor(() => expect(screen.getByText("小李")).toBeTruthy());

    fireEvent.click(screen.getByLabelText("移除 小李"));
    fireEvent.click(screen.getByText("取消"));

    expect(screen.queryByText("确定移出？")).toBeNull();
    expect(apiClient.removeMember).not.toHaveBeenCalled();
    expect(screen.getByText("小李")).toBeTruthy();
  });

  it("提升成员为管理员直接发请求，不确认", async () => {
    vi.mocked(apiClient.getWorkspace).mockResolvedValue(team);
    vi.mocked(apiClient.setMemberRole).mockResolvedValue({
      success: true,
      member: { id: "u2", name: "小李", role: "admin" },
      workspace: { ...team, adminCount: 2 },
    });
    render(<MemberPanel />);
    await waitFor(() => expect(screen.getByText("小李")).toBeTruthy());

    fireEvent.click(screen.getByText("设为管理员"));

    // 改角色是可逆的，多一次确认只是噪音
    await waitFor(() =>
      expect(apiClient.setMemberRole).toHaveBeenCalledWith("u2", "admin"),
    );
  });

  it("非管理员看不到任何操作入口", async () => {
    vi.mocked(apiClient.getWorkspace).mockResolvedValue({
      ...team,
      role: "user" as const,
      isAdmin: false,
      adminCount: 1,
      inviteCode: null,
      // 后端不给普通成员发管理字段
      members: [
        { id: "u1", name: "阿花", role: "admin" as const },
        { id: "u2", name: "小李", role: "user" as const },
      ],
    });
    render(<MemberPanel />);
    await waitFor(() =>
      expect(screen.getByText(/只有管理员能调整角色/)).toBeTruthy(),
    );

    expect(screen.queryByText("设为管理员")).toBeNull();
    expect(screen.queryByText("降为成员")).toBeNull();
    expect(screen.queryByLabelText("移除 小李")).toBeNull();
  });

  it("后端没给 adminCount 时退回自己数，禁用判断仍然成立", async () => {
    // 旧后端 + 新前端。缺字段不该让"最后一个管理员"那道禁用失效——
    // 失效的表现是按钮可点、点了报错，而用户不知道为什么
    const { adminCount: _omit, ...withoutCount } = team;
    vi.mocked(apiClient.getWorkspace).mockResolvedValue(withoutCount);
    render(<MemberPanel />);
    await waitFor(() => expect(screen.getByText("阿花")).toBeTruthy());

    expect((screen.getByText("降为成员") as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
});
