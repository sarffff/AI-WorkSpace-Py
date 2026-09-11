import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { FileHistoryPanel } from "./FileHistoryPanel";
import { apiClient } from "@/shared/api/client";

/**
 * 文件历史版本面板。
 *
 * 要钉住的核心性质是**两种"列表为空"必须说不同的话**：后端没开写工具时列表必然
 * 是空的，说"还没有写操作"会让人以为写过但没留下版本——而那两种情况用户该做的事
 * 完全不同（一个是去开开关，一个是不用管）。
 *
 * 第二条是"文件已不存在"要显眼：那是删除留下的备份，点恢复等于把文件建回来，
 * 和覆盖写的恢复不是一回事。
 */
vi.mock("@/shared/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn() }),
  toastMessageFrom: (e: unknown, fallback: string) =>
    e instanceof Error ? e.message : fallback,
}));

describe("FileHistoryPanel", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  beforeEach(() => {
    vi.spyOn(apiClient, "getFsBackups");
  });

  it("后端没开写工具时说的是「没开启」，不是「没写过」", async () => {
    vi.mocked(apiClient.getFsBackups).mockResolvedValue({
      backups: [],
      enabled: false,
    });

    render(<FileHistoryPanel />);

    await waitFor(() =>
      expect(screen.getByText(/没有开启文件写入功能/)).toBeTruthy(),
    );
    // 不该同时出现另一种说法
    expect(screen.queryByText(/还没有任何写操作/)).toBeNull();
  });

  it("开了但没写过时说的是「没写过」", async () => {
    vi.mocked(apiClient.getFsBackups).mockResolvedValue({
      backups: [],
      enabled: true,
    });

    render(<FileHistoryPanel />);

    await waitFor(() =>
      expect(screen.getByText(/还没有任何写操作/)).toBeTruthy(),
    );
    expect(screen.queryByText(/没有开启文件写入功能/)).toBeNull();
  });

  it("列出版本，动作名翻译成中文", async () => {
    vi.mocked(apiClient.getFsBackups).mockResolvedValue({
      enabled: true,
      backups: [
        {
          id: "b1",
          path: "C:/work/docs/notes.md",
          action: "write",
          size: 2048,
          createdAt: "2026-09-11T08:30:00",
          exists: true,
        },
      ],
    });

    render(<FileHistoryPanel />);

    await waitFor(() => expect(screen.getByText("docs/notes.md")).toBeTruthy());
    expect(screen.getByText("覆盖写")).toBeTruthy();
    expect(screen.getByText("2.0 KB")).toBeTruthy();
  });

  it("删除留下的备份要标出「文件已不存在」", async () => {
    // 点恢复等于把文件建回来，和覆盖写的恢复不是一回事，得看得出来
    vi.mocked(apiClient.getFsBackups).mockResolvedValue({
      enabled: true,
      backups: [
        {
          id: "b2",
          path: "C:/work/gone.md",
          action: "delete",
          size: 12,
          createdAt: "2026-09-11T08:31:00",
          exists: false,
        },
      ],
    });

    render(<FileHistoryPanel />);

    await waitFor(() => expect(screen.getByText("删除")).toBeTruthy());
    expect(screen.getByText("文件已不存在")).toBeTruthy();
  });

  it("点恢复会调接口并刷新列表", async () => {
    vi.mocked(apiClient.getFsBackups).mockResolvedValue({
      enabled: true,
      backups: [
        {
          id: "b3",
          path: "C:/work/notes.md",
          action: "write",
          size: 10,
          createdAt: "2026-09-11T08:32:00",
          exists: true,
        },
      ],
    });
    const restore = vi
      .spyOn(apiClient, "restoreFsBackup")
      .mockResolvedValue({ path: "C:/work/notes.md" });

    render(<FileHistoryPanel />);
    await waitFor(() => expect(screen.getByText("恢复")).toBeTruthy());

    fireEvent.click(screen.getByText("恢复"));

    expect(restore).toHaveBeenCalledWith("b3");
    // 恢复之后要重新拉一次：那次恢复自己也会产生一条 restore 备份
    await waitFor(() =>
      expect(vi.mocked(apiClient.getFsBackups).mock.calls.length).toBeGreaterThan(1),
    );
  });

  it("加载失败时显示错误而不是空列表", async () => {
    vi.mocked(apiClient.getFsBackups).mockRejectedValue(
      new Error("网络挂了"),
    );

    render(<FileHistoryPanel />);

    await waitFor(() => expect(screen.getByText("网络挂了")).toBeTruthy());
  });
});
