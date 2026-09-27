import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ReviewsPage } from "./ReviewsPage";
import type { ReviewLedgerItem } from "@/shared/types/api.types";

/**
 * 审核台账页面。
 *
 * 要钉的都是"不说出来就会被误解"的性质，不是渲染细节：
 * 1. **默认落在待办、且筛选真的传给后端。** needs_human 没有"待办在哪"的入口，
 *    转人工就等于把结论扔进没人看的队列——这是这页存在的一半理由。
 * 2. **开关关掉和"还没审过"要分得开。** 两者都空，但处置完全不同。
 * 3. **runs=1 读作"未做独立复审"，不是"复审过且一致"。**
 * 4. **复核接线：点复核 → 弹窗 → resolveReview 真的带着处置发出去。**
 * 5. **已复核的显示处置、不再给复核入口**（台账要能作依据，不能反复改写）。
 */

vi.mock("@/shared/api/client", () => ({
  apiClient: { getReviews: vi.fn(), resolveReview: vi.fn() },
}));

vi.mock("@/shared/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  toastMessageFrom: (e: unknown, fallback: string) =>
    e instanceof Error ? e.message : fallback,
}));

const { apiClient } = await import("@/shared/api/client");

function renderPage() {
  return render(
    <MemoryRouter>
      <ReviewsPage />
    </MemoryRouter>,
  );
}

function makeItem(over: Partial<ReviewLedgerItem> = {}): ReviewLedgerItem {
  return {
    id: "r1",
    subject: "张三 3 月住宿",
    sopName: "expense-review",
    sopVersion: 0,
    verdict: "needs_human",
    inputs: [{ name: "凭证", value: null, found: false }],
    basis: ["额度标准：一线 600"],
    runs: 1,
    agreed: true,
    evidence: "住宿 480 元",
    createdAt: "2026-09-20T10:00:00",
    chatId: "c1",
    resolvedBy: null,
    resolvedAt: null,
    resolution: null,
    resolutionNote: null,
    ...over,
  };
}

describe("ReviewsPage", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.mocked(apiClient.getReviews).mockReset();
    vi.mocked(apiClient.resolveReview).mockReset();
  });

  it("默认落在待办，并把筛选真的传给后端", async () => {
    vi.mocked(apiClient.getReviews).mockResolvedValue({
      items: [makeItem()],
      enabled: true,
    });
    renderPage();

    // 挂载即拉待办：pending_only=true
    await waitFor(() =>
      expect(vi.mocked(apiClient.getReviews)).toHaveBeenCalledWith(true),
    );

    // 切到「全部」要传 false，否则待办筛选形同虚设
    fireEvent.click(screen.getByText("全部"));
    await waitFor(() =>
      expect(vi.mocked(apiClient.getReviews)).toHaveBeenCalledWith(false),
    );
  });

  it("开关关掉时说清未开启，而不是当成空台账", async () => {
    vi.mocked(apiClient.getReviews).mockResolvedValue({
      items: [],
      enabled: false,
    });
    renderPage();

    await waitFor(() =>
      expect(screen.getByText("审核台账未开启")).toBeTruthy(),
    );
  });

  it("runs=1 显示为“未做独立复审”，不冒充复审通过", async () => {
    vi.mocked(apiClient.getReviews).mockResolvedValue({
      items: [makeItem({ runs: 1, agreed: true })],
      enabled: true,
    });
    renderPage();

    await waitFor(() =>
      expect(screen.getByText(/未做独立复审/)).toBeTruthy(),
    );
  });

  it("复核：点复核弹窗、处置真的带着结论发出去", async () => {
    vi.mocked(apiClient.getReviews).mockResolvedValue({
      items: [makeItem()],
      enabled: true,
    });
    vi.mocked(apiClient.resolveReview).mockResolvedValue(
      makeItem({ resolvedBy: "u1", resolution: "approved" }),
    );
    renderPage();

    await waitFor(() => expect(screen.getByText("复核")).toBeTruthy());
    fireEvent.click(screen.getByText("复核"));

    // 弹窗出来后提交
    const submit = await waitFor(() =>
      screen.getByRole("button", { name: "记下处置" }),
    );
    fireEvent.click(submit);

    await waitFor(() =>
      expect(vi.mocked(apiClient.resolveReview)).toHaveBeenCalledWith(
        "r1",
        "approved",
        "",
      ),
    );
  });

  it("已复核的显示处置、不再给复核入口", async () => {
    vi.mocked(apiClient.getReviews).mockResolvedValue({
      items: [
        makeItem({
          resolvedBy: "admin",
          resolvedAt: "2026-09-21T09:00:00",
          resolution: "approved",
          resolutionNote: "我看过了",
        }),
      ],
      enabled: true,
    });
    renderPage();

    await waitFor(() => expect(screen.getByText(/已批准/)).toBeTruthy());
    expect(screen.getByText(/我看过了/)).toBeTruthy();
    expect(screen.queryByText("复核")).toBeNull();
  });
});
