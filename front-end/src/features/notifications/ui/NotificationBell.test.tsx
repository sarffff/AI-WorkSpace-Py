import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { AppNotification } from "@/shared/types/api.types";

/**
 * 通知铃铛。
 *
 * 钉的是最容易回归的几条：红点来自 unread_count 轮询（而不是本页条数）、
 * 打开面板才拉正文、点一条会**同时**标已读并跳到那件事的现场。
 *
 * 跳转目标是工单而不是会话：一次审批可能挂一整天，中间没有任何连接活着，
 * 所以通知是"重新发现那件事"的唯一路径。健康告警没有 ticketId，它指向指标看板。
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
  isConflictResponse: (value: unknown) =>
    typeof value === "object" && value !== null && "conflict" in value,
}));

const { apiClient } = await import("@/shared/api/client");
const { NotificationBell } = await import("./NotificationBell");

const note = (over: Partial<AppNotification> = {}): AppNotification => ({
  id: "n1",
  kind: "approval_required",
  title: "等你批：发起退款",
  body: "订单 ORD-1 退款 350 元",
  ticketId: "t-1",
  createdAt: new Date().toISOString(),
  readAt: null,
  read: false,
  ...over,
});

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

const renderBell = () =>
  render(
    <MemoryRouter initialEntries={["/queue"]}>
      <NotificationBell />
      <Routes>
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>
  );

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
    expect(await screen.findByLabelText(/3 条未读/)).toBeTruthy();
  });

  it("打开面板才拉正文，平时只轮询数字", async () => {
    vi.mocked(apiClient.getUnreadCount).mockResolvedValue(1);
    vi.mocked(apiClient.getNotifications).mockResolvedValue({
      notifications: [note()],
      unreadCount: 1,
    });
    renderBell();
    expect(apiClient.getNotifications).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByLabelText(/1 条未读/));
    expect(await screen.findByText("等你批：发起退款")).toBeTruthy();
    expect(apiClient.getNotifications).toHaveBeenCalledTimes(1);
  });

  it("点一条带工单的通知：标已读并跳到那张单", async () => {
    vi.mocked(apiClient.getUnreadCount).mockResolvedValue(1);
    vi.mocked(apiClient.getNotifications).mockResolvedValue({
      notifications: [note({ id: "n9", ticketId: "t-42" })],
      unreadCount: 1,
    });
    renderBell();
    fireEvent.click(await screen.findByLabelText(/1 条未读/));
    fireEvent.click(await screen.findByText("等你批：发起退款"));

    expect(apiClient.markNotificationRead).toHaveBeenCalledWith("n9");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/tickets/t-42")
    );
  });

  it("健康告警没有工单可跳，指向指标看板", async () => {
    vi.mocked(apiClient.getUnreadCount).mockResolvedValue(1);
    vi.mocked(apiClient.getNotifications).mockResolvedValue({
      notifications: [note({ id: "h1", kind: "health_alert", ticketId: null })],
      unreadCount: 1,
    });
    renderBell();
    fireEvent.click(await screen.findByLabelText(/1 条未读/));
    fireEvent.click(await screen.findByText("等你批：发起退款"));

    expect(apiClient.markNotificationRead).toHaveBeenCalledWith("h1");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/metrics")
    );
  });

  it("全部已读把红点清零", async () => {
    vi.mocked(apiClient.getUnreadCount).mockResolvedValue(2);
    vi.mocked(apiClient.getNotifications).mockResolvedValue({
      notifications: [note({ id: "a" }), note({ id: "b", ticketId: null })],
      unreadCount: 2,
    });
    renderBell();
    fireEvent.click(await screen.findByLabelText(/2 条未读/));
    fireEvent.click(await screen.findByText("全部已读"));

    expect(apiClient.markAllNotificationsRead).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByLabelText(/条未读/)).toBeNull());
  });
});
