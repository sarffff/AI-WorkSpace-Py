import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { Provider } from "react-redux";
import { MemoryRouter } from "react-router-dom";
import { store } from "@/app/providers/store";
import type { AppNotification } from "@/shared/types/api.types";

/**
 * 通知铃铛。补 B1 的洞——后端早已上线、前端此前零消费，所以这里钉的是最容易回归的
 * 几条：红点来自 unread_count 轮询、打开才拉正文、点带 chatId 的通知要标已读并切到
 * 那个会话（HITL 闭环的最后一环）、全部已读把红点清零。
 */
vi.mock("@/shared/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  toastMessageFrom: (e: unknown, fb: string) =>
    e instanceof Error ? e.message : fb,
}));
vi.mock("@/shared/api/client", () => ({
  apiClient: {
    getUnreadCount: vi.fn(),
    getNotifications: vi.fn(),
    markNotificationRead: vi.fn(),
    markAllNotificationsRead: vi.fn(),
  },
}));

const { apiClient } = await import("@/shared/api/client");
const { NotificationBell } = await import("./NotificationBell");

const note = (over: Partial<AppNotification> = {}): AppNotification => ({
  id: "n1",
  kind: "approval_required",
  title: "待审批：delete_file",
  body: "删除 D:/x.md",
  runId: "r1",
  chatId: "c1",
  createdAt: new Date().toISOString(),
  readAt: null,
  read: false,
  ...over,
});

const renderBell = () =>
  render(
    <Provider store={store}>
      <MemoryRouter>
        <NotificationBell />
      </MemoryRouter>
    </Provider>,
  );

/* TEST_MARKER */
describe("NotificationBell", () => {
  beforeEach(() => {
    vi.mocked(apiClient.getUnreadCount).mockResolvedValue(0);
    vi.mocked(apiClient.getNotifications).mockResolvedValue({
      notifications: [],
      unreadCount: 0,
    });
    vi.mocked(apiClient.markNotificationRead).mockResolvedValue(undefined);
    vi.mocked(apiClient.markAllNotificationsRead).mockResolvedValue(0);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("红点显示轮询回来的未读数", async () => {
    vi.mocked(apiClient.getUnreadCount).mockResolvedValue(3);
    renderBell();
    // 挂载即轮询一次，红点读的是未读总数（不是本页条数）
    expect(await screen.findByLabelText(/3 条未读/)).toBeTruthy();
  });

  it("打开才拉正文；平时只轮询数字", async () => {
    vi.mocked(apiClient.getUnreadCount).mockResolvedValue(1);
    vi.mocked(apiClient.getNotifications).mockResolvedValue({
      notifications: [note()],
      unreadCount: 1,
    });
    renderBell();
    expect(apiClient.getNotifications).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByLabelText(/通知/));
    expect(await screen.findByText("待审批：delete_file")).toBeTruthy();
    expect(apiClient.getNotifications).toHaveBeenCalledTimes(1);
  });

  it("点带 chatId 的通知：标记已读并切到那个会话", async () => {
    vi.mocked(apiClient.getUnreadCount).mockResolvedValue(1);
    vi.mocked(apiClient.getNotifications).mockResolvedValue({
      notifications: [note({ id: "n9", chatId: "chat-42" })],
      unreadCount: 1,
    });
    renderBell();
    fireEvent.click(await screen.findByLabelText(/通知/));
    fireEvent.click(await screen.findByText("待审批：delete_file"));

    expect(apiClient.markNotificationRead).toHaveBeenCalledWith("n9");
    // HITL 闭环：切到那个会话，待审批卡片才会在 ChatPage 重新浮出
    expect(store.getState().chat.currentChatId).toBe("chat-42");
  });

  it("全部已读把红点清零", async () => {
    vi.mocked(apiClient.getUnreadCount).mockResolvedValue(2);
    vi.mocked(apiClient.getNotifications).mockResolvedValue({
      notifications: [note({ id: "a" }), note({ id: "b", chatId: null })],
      unreadCount: 2,
    });
    renderBell();
    fireEvent.click(await screen.findByLabelText(/2 条未读/));
    fireEvent.click(await screen.findByText("全部已读"));

    expect(apiClient.markAllNotificationsRead).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByLabelText(/条未读/)).toBeNull(),
    );
  });
});
