import { describe, it, expect, vi, afterEach } from "vitest";
// 用 fireEvent 而不是 user-event：后者没装，而这里要模拟的动作（点按钮、
// 改输入框的值）fireEvent 都够，不值得为它加一个依赖。
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { ToolApprovalCard } from "./ToolApprovalCard";

/**
 * 审批卡片的编辑态。
 *
 * ## 为什么这里必须有测试
 *
 * 卡片上显示的参数**不是**将要执行的参数。它来自后端 `approval.build_preview`，
 * 那个函数对字符串做了两件有损的事：
 *
 * 1. `mask_markup` 中和标记语法（模型写的内容可能整段来自它刚抓的网页）
 * 2. 截断到 800 字，并把原文长度记在 `<键>__chars` 里
 *
 * 于是"让用户编辑参数"这件事天然带一个静默的数据损坏路径：把卡片上的值原样
 * 回传，就是用有损副本覆盖原文。一次"只改标题"的操作会把两千字正文变成八百字的
 * 脱敏版本——**而界面上完全看不出来**，模型还会照常报告"已保存"。
 *
 * 所以这里测的不是"输入框能不能打字"，是那两条防线：
 * - 截断字段不给编辑
 * - 只回传值真的变了的键
 */

afterEach(cleanup);

const PREVIEW = {
  name: "季度总结草稿",
  content: "开头这一段…",
  // 后端对超长字符串会额外给这个键，值是原文长度
  content__chars: 2000,
};

describe("审批卡片的编辑态", () => {
  it("默认不是编辑态，同意时不带任何改动", async () => {
    const onDecide = vi.fn();
    render(
      <ToolApprovalCard
        tool="save_to_knowledge_base"
        preview={{ name: "草稿" }}
        onDecide={onDecide}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /同意执行/ }));

    expect(onDecide).toHaveBeenCalledWith(true, "", undefined);
  });

  it("截断的字段不给编辑，并说明为什么", async () => {
    // 卡片上只有开头 800 字。在这上面改再提交等于砍掉后面 1200 字，
    // 而用户以为自己只是改了个错别字。要改长正文得让模型重写（拒绝并说明），
    // 那条路不丢东西。
    render(
      <ToolApprovalCard
        tool="save_to_knowledge_base"
        preview={PREVIEW}
        onDecide={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /改一下/ }));

    const boxes = screen.getAllByRole("textbox");
    // name 可改，content 被截断所以不出现输入框
    expect(boxes).toHaveLength(1);
    expect((boxes[0] as HTMLInputElement).value).toBe("季度总结草稿");
    expect(screen.getByText(/不能在这里改/)).toBeTruthy();
  });

  it("只把值真的变了的键回传", async () => {
    const onDecide = vi.fn();
    render(
      <ToolApprovalCard
        tool="save_to_knowledge_base"
        preview={PREVIEW}
        onDecide={onDecide}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /改一下/ }));
    const input = screen.getAllByRole("textbox")[0];
    fireEvent.change(input, { target: { value: "Q3 复盘" } });
    fireEvent.click(screen.getByRole("button", { name: /按改动执行/ }));

    expect(onDecide).toHaveBeenCalledWith(true, "", { name: "Q3 复盘" });
    // 截断的那个键绝不能出现——它是整条链上唯一会静默丢数据的地方
    expect(onDecide.mock.calls[0][2]).not.toHaveProperty("content");
  });

  it("改回原值等于没改", async () => {
    // 点进输入框再改回去也会在 edits 里留一条记录。按 `key in edits` 判断的话
    // 会白触发一次"参数被人改过"的说明回灌给模型，而实际什么都没变——
    // 模型于是对用户说"按你改的存好了"，用户却没改任何东西。
    const onDecide = vi.fn();
    render(
      <ToolApprovalCard
        tool="save_to_knowledge_base"
        preview={{ name: "草稿" }}
        onDecide={onDecide}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /改一下/ }));
    const input = screen.getAllByRole("textbox")[0];
    // 必须先改成别的、再改回来。直接把值设成原样的话 React 不会触发 onChange
    // （值没变），edits 里压根不会留下记录——那样这条测试就绕过了它要测的东西，
    // 无论实现对不对都会绿。
    fireEvent.change(input, { target: { value: "改坏了" } });
    fireEvent.change(input, { target: { value: "草稿" } });
    fireEvent.click(screen.getByRole("button", { name: /同意执行/ }));

    expect(onDecide).toHaveBeenCalledWith(true, "", undefined);
  });

  it("全是截断字段时不给编辑入口", async () => {
    // 给一个点进去什么都改不了的按钮，比没有这个按钮更糟。
    render(
      <ToolApprovalCard
        tool="save_to_knowledge_base"
        preview={{ content: "开头…", content__chars: 2000 }}
        onDecide={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /改一下/ })).toBeNull();
  });

  it("拒绝时不带改动——那是个自相矛盾的请求", async () => {
    // 后端对 approved=false + editedArguments 直接 422：不执行的调用没有参数可言。
    // 静默忽略那个字段会让客户端的 bug 变成"我改了但没生效"。
    const onDecide = vi.fn();
    render(
      <ToolApprovalCard
        tool="save_to_knowledge_base"
        preview={{ name: "草稿" }}
        onDecide={onDecide}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /^不执行/ }));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "知识库里已经有了" },
    });
    fireEvent.click(screen.getByRole("button", { name: /确认不执行/ }));

    expect(onDecide).toHaveBeenCalledWith(false, "知识库里已经有了");
  });
});

describe("ToolApprovalCard 的改动预览", () => {
  afterEach(cleanup);

  /**
   * 文件操作的参数写着"把 old_text 换成 new_text"，那看不出这次改动到底动了什么。
   * diff 是后端算的（前端没有文件访问权），这里测的是它有没有被渲染出来、
   * 以及有没有被当成一个普通参数渲染成一行 JSON。
   */
  const base = {
    tool: "edit_file",
    onDecide: vi.fn(),
  };

  it("渲染 diff 而不是把它当参数显示成 JSON", () => {
    render(
      <ToolApprovalCard
        {...base}
        preview={{
          path: "work/config.ini",
          __diff: ["--- 修改前", "+++ 修改后", "-port=8080", "+port=9090"],
          __diff_truncated: false,
        }}
      />,
    );
    expect(screen.getByText("-port=8080")).toBeTruthy();
    expect(screen.getByText("+port=9090")).toBeTruthy();
    expect(screen.getByText("这次会做的改动")).toBeTruthy();
    // __diff 不能出现在参数表里——那会是一行 ["--- 修改前","+++ 修改后",...]
    expect(screen.queryByText("__diff")).toBeNull();
  });

  it("diff 之外的真实参数照样显示", () => {
    // 用户既要看到改了什么，也要看到改的是哪个文件
    render(
      <ToolApprovalCard
        {...base}
        preview={{
          path: "work/config.ini",
          __diff: ["-a", "+b"],
        }}
      />,
    );
    expect(screen.getByText("work/config.ini")).toBeTruthy();
    expect(screen.getByText("文件")).toBeTruthy();
  });

  it("截断时说明只显示了开头一段", () => {
    render(
      <ToolApprovalCard
        {...base}
        preview={{ path: "x", __diff: ["-a"], __diff_truncated: true }}
      />,
    );
    expect(screen.getByText(/只显示开头一段/)).toBeTruthy();
  });

  it("新建文件给行数而不是空的 diff 块", () => {
    render(
      <ToolApprovalCard
        {...base}
        tool="write_file"
        preview={{ path: "work/new.md", __new_file: true, __lines: 12 }}
      />,
    );
    expect(screen.getByText(/新文件，共 12 行/)).toBeTruthy();
    expect(screen.queryByText("这次会做的改动")).toBeNull();
  });

  it("没有 diff 时不渲染改动块", () => {
    // 删除操作、知识库写入都不带 diff
    render(
      <ToolApprovalCard {...base} tool="delete_file" preview={{ path: "x" }} />,
    );
    expect(screen.queryByText("这次会做的改动")).toBeNull();
  });

  it("__ 前缀的键不进可编辑字段", () => {
    // 进了的话"改一下"会给出一个编辑 diff 的输入框，而 diff 不是参数
    render(
      <ToolApprovalCard
        {...base}
        preview={{ path: "work/x.md", __diff: ["-a", "+b"] }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /改一下/ }));
    const inputs = screen.getAllByRole("textbox");
    expect(inputs).toHaveLength(1);
  });
});
