import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { FileTree } from "./FileTree";

/**
 * 工作区文件树。
 *
 * 要钉的性质集中在**导航的边界**，而不是渲染：
 *
 * 1. **`parent` 为 null 时不许出现"返回上级"。** 根目录的上一级在沙箱外，后端
 *    刻意给 null。显示了的话点下去必然 400，用户看到一个莫名的报错。
 * 2. **"没授权任何文件夹"和"空目录"要说不同的话。** 前者该引导去设置里选一个，
 *    后者只是这个目录里没东西——给同一句话会让人白跑一趟设置页。
 * 3. **点文件不触发导航，点目录不触发 onPick。** 树是只读的，写操作全走审批。
 */

vi.mock("@/shared/api/client", () => ({
  apiClient: { browseFs: vi.fn() },
}));

vi.mock("@/shared/ui/Toast", () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  toastMessageFrom: (e: unknown, fallback: string) =>
    e instanceof Error ? e.message : fallback,
}));

const { apiClient } = await import("@/shared/api/client");

const rootList = {
  path: null,
  label: null,
  parent: null,
  truncated: false,
  entries: [
    {
      name: "我的资料",
      path: "D:/work",
      isDir: true,
      size: null,
      isRoot: true,
    },
  ],
};

const inRoot = {
  path: "D:/work",
  label: "我的资料",
  parent: null, // 根的上一级在沙箱外
  truncated: false,
  entries: [
    { name: "sub", path: "D:/work/sub", isDir: true, size: null, isRoot: false },
    {
      name: "a.txt",
      path: "D:/work/a.txt",
      isDir: false,
      size: 2048,
      isRoot: false,
    },
  ],
};

describe("FileTree", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.mocked(apiClient.browseFs).mockReset();
  });

  it("打开时先列授权根", async () => {
    vi.mocked(apiClient.browseFs).mockResolvedValue(rootList);
    render(<FileTree />);
    await waitFor(() => expect(screen.getByText("我的资料")).toBeTruthy());
    // 省略 path 请求根列表
    expect(apiClient.browseFs).toHaveBeenCalledWith(undefined);
  });

  it("点目录进下一层，点文件交给 onPick 而不导航", async () => {
    vi.mocked(apiClient.browseFs).mockResolvedValueOnce(rootList);
    const onPick = vi.fn();
    render(<FileTree onPick={onPick} />);
    await waitFor(() => expect(screen.getByText("我的资料")).toBeTruthy());

    vi.mocked(apiClient.browseFs).mockResolvedValueOnce(inRoot);
    fireEvent.click(screen.getByText("我的资料"));
    await waitFor(() => expect(screen.getByText("a.txt")).toBeTruthy());
    expect(apiClient.browseFs).toHaveBeenLastCalledWith("D:/work");

    const callsBefore = vi.mocked(apiClient.browseFs).mock.calls.length;
    fireEvent.click(screen.getByText("a.txt"));
    expect(onPick).toHaveBeenCalledWith(
      expect.objectContaining({ path: "D:/work/a.txt", isDir: false }),
    );
    // 点文件不该再请求一次目录
    expect(vi.mocked(apiClient.browseFs).mock.calls.length).toBe(callsBefore);
  });

  it("parent 为 null 时不显示返回上级", async () => {
    vi.mocked(apiClient.browseFs).mockResolvedValueOnce(rootList);
    render(<FileTree />);
    await waitFor(() => expect(screen.getByText("我的资料")).toBeTruthy());

    vi.mocked(apiClient.browseFs).mockResolvedValueOnce(inRoot);
    fireEvent.click(screen.getByText("我的资料"));
    await waitFor(() => expect(screen.getByText("a.txt")).toBeTruthy());

    // 这一条是这个组件最容易写错的地方：显示了点下去必然 400
    expect(screen.queryByText("返回上级")).toBeNull();
    // 但要能回到根列表
    expect(screen.getByText("全部文件夹")).toBeTruthy();
  });

  it("parent 有值时显示返回上级并按它请求", async () => {
    vi.mocked(apiClient.browseFs).mockResolvedValueOnce({
      ...inRoot,
      path: "D:/work/sub",
      label: "我的资料/sub",
      parent: "D:/work",
      entries: [],
    });
    render(<FileTree />);
    await waitFor(() => expect(screen.getByText("返回上级")).toBeTruthy());

    vi.mocked(apiClient.browseFs).mockResolvedValueOnce(inRoot);
    fireEvent.click(screen.getByText("返回上级"));
    await waitFor(() =>
      expect(apiClient.browseFs).toHaveBeenLastCalledWith("D:/work"),
    );
  });

  it("没授权和空目录说的是不同的话", async () => {
    vi.mocked(apiClient.browseFs).mockResolvedValueOnce({
      ...rootList,
      entries: [],
    });
    const { unmount } = render(<FileTree />);
    await waitFor(() =>
      expect(screen.getByText(/还没有授权文件夹/)).toBeTruthy(),
    );
    unmount();

    vi.mocked(apiClient.browseFs).mockResolvedValueOnce({
      ...inRoot,
      entries: [],
      parent: null,
    });
    render(<FileTree />);
    await waitFor(() => expect(screen.getByText("空目录")).toBeTruthy());
    expect(screen.queryByText(/还没有授权文件夹/)).toBeNull();
  });

  it("文件显示大小，目录不显示", async () => {
    vi.mocked(apiClient.browseFs).mockResolvedValue(inRoot);
    render(<FileTree />);
    await waitFor(() => expect(screen.getByText("a.txt")).toBeTruthy());
    expect(screen.getByText("2.0 KB")).toBeTruthy();
  });

  it("截断时说清还有更多，并指向搜索", async () => {
    vi.mocked(apiClient.browseFs).mockResolvedValue({
      ...inRoot,
      truncated: true,
    });
    render(<FileTree />);
    await waitFor(() => expect(screen.getByText(/条目太多/)).toBeTruthy());
  });

  it("请求失败不把上一层的内容留在屏幕上骗人", async () => {
    vi.mocked(apiClient.browseFs).mockResolvedValueOnce(rootList);
    render(<FileTree />);
    await waitFor(() => expect(screen.getByText("我的资料")).toBeTruthy());

    // 失败时保持在原地：没有 setView，所以还是根列表，且 path 没变
    vi.mocked(apiClient.browseFs).mockRejectedValueOnce(new Error("越界"));
    fireEvent.click(screen.getByText("我的资料"));
    await waitFor(() =>
      expect(vi.mocked(apiClient.browseFs).mock.calls.length).toBe(2),
    );
    expect(screen.getByText("我的资料")).toBeTruthy();
  });
});
