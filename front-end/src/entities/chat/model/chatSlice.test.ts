import { describe, it, expect } from "vitest";
import reducer, {
  addMessage,
  appendToMessage,
  deleteChat,
  removeMessage,
  renameChat,
  setCurrentChat,
  setSessions,
  togglePinChat,
  updateMessageContent,
} from "./chatSlice";
import type { ChatSession, UIMessage } from "@/shared/types/api.types";

/**
 * 对话状态的 reducer。选它是因为流式对话的每一个增量都要过 appendToMessage，
 * 而这一族 reducer 的失效方式是"消息串到别的会话去"或"删会话删不干净"——
 * 都不抛异常，只是界面上少一条或多一条。
 */

const session = (id: string, extra: Partial<ChatSession> = {}): ChatSession =>
  ({ id, title: id, pinned: false, ...extra }) as ChatSession;

const message = (
  id: string,
  sessionId: string,
  content = "",
): UIMessage => ({ id, sessionId, role: "assistant", content }) as UIMessage;

/** 从 initialState 出发建一个带会话与消息的状态。 */
const withData = () => {
  let state = reducer(undefined, setSessions([session("s1"), session("s2")]));
  state = reducer(state, addMessage(message("m1", "s1", "你")));
  state = reducer(state, addMessage(message("m2", "s2", "另一个会话")));
  return state;
};

describe("会话管理", () => {
  it("setCurrentChat 传 id 时顺带切到 chat 页", () => {
    const state = reducer(undefined, setCurrentChat("s1"));
    expect(state.currentChatId).toBe("s1");
    expect(state.activeTab).toBe("chat");
  });

  it("setCurrentChat 传 null 不改变当前页签", () => {
    // 清空当前会话不该把用户从设置页拽回对话页。
    let state = reducer(undefined, setSessions([session("s1")]));
    state = { ...state, activeTab: "settings" };
    const next = reducer(state, setCurrentChat(null));
    expect(next.currentChatId).toBeNull();
    expect(next.activeTab).toBe("settings");
  });

  it("deleteChat 同时删掉该会话的消息", () => {
    // 只删 sessions 不删 messagesBySession 的话，新建同 id 会话会看到旧消息，
    // 而且内存里那份永远不释放。
    const state = reducer(withData(), deleteChat("s1"));
    expect(state.sessions.map((s) => s.id)).toEqual(["s2"]);
    expect(state.messagesBySession).not.toHaveProperty("s1");
    expect(state.messagesBySession.s2).toHaveLength(1);
  });

  it("删掉当前会话时自动选中剩下的第一个", () => {
    let state = withData();
    state = reducer(state, setCurrentChat("s1"));
    const next = reducer(state, deleteChat("s1"));
    expect(next.currentChatId).toBe("s2");
  });

  it("删掉最后一个会话时 currentChatId 变 null", () => {
    let state = reducer(undefined, setSessions([session("only")]));
    state = reducer(state, setCurrentChat("only"));
    const next = reducer(state, deleteChat("only"));
    expect(next.currentChatId).toBeNull();
  });

  it("删掉非当前会话不改变当前选中", () => {
    let state = withData();
    state = reducer(state, setCurrentChat("s2"));
    const next = reducer(state, deleteChat("s1"));
    expect(next.currentChatId).toBe("s2");
  });

  it("renameChat 只改目标会话", () => {
    const state = reducer(withData(), renameChat({ id: "s2", title: "新标题" }));
    expect(state.sessions.find((s) => s.id === "s2")?.title).toBe("新标题");
    expect(state.sessions.find((s) => s.id === "s1")?.title).toBe("s1");
  });

  it("renameChat 对不存在的 id 是空操作，不抛错", () => {
    const before = withData();
    const after = reducer(before, renameChat({ id: "nope", title: "x" }));
    expect(after.sessions).toEqual(before.sessions);
  });

  it("togglePinChat 来回切换", () => {
    let state = reducer(withData(), togglePinChat("s1"));
    expect(state.sessions.find((s) => s.id === "s1")?.pinned).toBe(true);
    state = reducer(state, togglePinChat("s1"));
    expect(state.sessions.find((s) => s.id === "s1")?.pinned).toBe(false);
  });
});

describe("消息管理", () => {
  it("addMessage 给新会话自动建数组", () => {
    const state = reducer(undefined, addMessage(message("m1", "brand-new")));
    expect(state.messagesBySession["brand-new"]).toHaveLength(1);
  });

  it("appendToMessage 逐块拼接（流式对话的主路径）", () => {
    let state = withData();
    state = reducer(
      state,
      appendToMessage({ id: "m1", sessionId: "s1", content: "好" }),
    );
    state = reducer(
      state,
      appendToMessage({ id: "m1", sessionId: "s1", content: "，世界" }),
    );
    expect(state.messagesBySession.s1[0].content).toBe("你好，世界");
  });

  it("appendToMessage 不会串到同名 id 的其它会话", () => {
    // sessionId 与 id 都要匹配。少判一个的话，两个会话同时流式输出时
    // 内容会互相污染——而这在真实使用里就是"开两个标签页"。
    let state = reducer(undefined, addMessage(message("same", "s1", "A")));
    state = reducer(state, addMessage(message("same", "s2", "B")));
    state = reducer(
      state,
      appendToMessage({ id: "same", sessionId: "s1", content: "+" }),
    );
    expect(state.messagesBySession.s1[0].content).toBe("A+");
    expect(state.messagesBySession.s2[0].content).toBe("B");
  });

  it("appendToMessage 对不存在的会话是空操作", () => {
    const before = withData();
    const after = reducer(
      before,
      appendToMessage({ id: "m1", sessionId: "ghost", content: "x" }),
    );
    expect(after.messagesBySession).toEqual(before.messagesBySession);
  });

  it("appendToMessage 对不存在的消息 id 是空操作", () => {
    const before = withData();
    const after = reducer(
      before,
      appendToMessage({ id: "ghost", sessionId: "s1", content: "x" }),
    );
    expect(after.messagesBySession.s1[0].content).toBe("你");
  });

  it("updateMessageContent 整条替换而不是追加", () => {
    const state = reducer(
      withData(),
      updateMessageContent({ id: "m1", sessionId: "s1", content: "替换后" }),
    );
    expect(state.messagesBySession.s1[0].content).toBe("替换后");
  });

  it("removeMessage 只删指定那条", () => {
    let state = withData();
    state = reducer(state, addMessage(message("m1b", "s1", "第二条")));
    const next = reducer(
      state,
      removeMessage({ sessionId: "s1", messageId: "m1" }),
    );
    expect(next.messagesBySession.s1.map((m) => m.id)).toEqual(["m1b"]);
  });

  it("removeMessage 对不存在的会话是空操作", () => {
    const before = withData();
    const after = reducer(
      before,
      removeMessage({ sessionId: "ghost", messageId: "m1" }),
    );
    expect(after.messagesBySession).toEqual(before.messagesBySession);
  });

  it("消息保持插入顺序", () => {
    let state = reducer(undefined, addMessage(message("a", "s")));
    state = reducer(state, addMessage(message("b", "s")));
    state = reducer(state, addMessage(message("c", "s")));
    expect(state.messagesBySession.s.map((m) => m.id)).toEqual([
      "a",
      "b",
      "c",
    ]);
  });
});
