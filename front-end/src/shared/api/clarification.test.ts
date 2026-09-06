import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { ApiClient } from "./client";

/**
 * 澄清接续：`POST /chats/runs/{runId}/answer`。
 *
 * ## 为什么这条链值得单独测
 *
 * 在 2026-08-30 之前，后端把 `ask_user` 整条链都做完了（挂起、存快照、
 * `waiting_input`、`/answer` 端点），前端却**只有类型声明**——`clarification`
 * 在 `api.types.ts` 里出现 5 次，全在注释和联合类型里，没有组件、没有接口调用。
 * 类型注释甚至写着"`true` 时该调 POST /chats/runs/{runId}/answer"，而那个调用
 * 不存在。
 *
 * 实际后果：模型调 `ask_user` 提问，后端挂起等答案，SSE 里那个 `clarification`
 * 事件在主循环里匹配不到任何分支、被静默丢掉。用户看到的是"生成突然停了"，
 * 没有任何可点的东西，那个 run 就一直挂在 `waiting_input` 里。
 *
 * 这是这个项目反复出现的形状——**一侧做完了，另一侧没接上**——所以这里测的是
 * 接口契约本身：请求发到哪、载荷长什么样、409 怎么表达。
 */

const streamOf = (...chunks: string[]): ReadableStream<Uint8Array> => {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
};

const okResponse = (body: ReadableStream<Uint8Array>) =>
  ({ ok: true, status: 200, statusText: "OK", body }) as unknown as Response;

const statusResponse = (status: number, statusText = "") =>
  ({ ok: false, status, statusText, body: null }) as unknown as Response;

describe("澄清接续", () => {
  let client: ApiClient;

  beforeEach(() => {
    localStorage.clear();
    client = new ApiClient("http://test.invalid");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("把答案 POST 到该去的那个 run 上", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okResponse(streamOf('data: {"type":"done"}\n')));
    vi.stubGlobal("fetch", fetchMock);

    for await (const _ of client.answerClarification("run-7", "用季度那份")) {
      void _;
    }

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/chats/runs/run-7/answer");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ answer: "用季度那份" });
  });

  it("走的是同一套 SSE 事件，能接着流式输出", async () => {
    // 接续之后模型还要继续调工具、继续吐正文。前端因此可以复用同一个渲染
    // 循环——抄第二份的代价是以后往流里加字段得改两处。
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        okResponse(
          streamOf(
            'data: {"type":"clarification_answered"}\n',
            'data: {"type":"message_delta","content":"好"}\n',
            'data: {"type":"done"}\n',
          ),
        ),
      ),
    );

    const out: unknown[] = [];
    for await (const chunk of client.answerClarification("run-7", "是")) {
      out.push(chunk);
    }
    expect(out).toEqual([
      { type: "clarification_answered" },
      { type: "message_delta", content: "好" },
      { type: "done" },
    ]);
  });

  it("409 抛 STALE_CLARIFICATION 而不是通用错误", async () => {
    // 409 = 这个 run 已经不在 waiting_input 了：别的标签页答过，或者挂太久被
    // 标成 abandoned。这**不是**错误，调用方据此把卡片收掉并提示一句就行。
    // 和通用失败混在一起的话，用户会为一件已经处理完的事看到一个红色报错。
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(statusResponse(409)));

    await expect(async () => {
      for await (const _ of client.answerClarification("run-7", "是")) {
        void _;
      }
    }).rejects.toThrow("STALE_CLARIFICATION");
  });

  it("其他失败状态码保持原样报错", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(statusResponse(500, "Internal Server Error")),
    );

    await expect(async () => {
      for await (const _ of client.answerClarification("run-7", "是")) {
        void _;
      }
    }).rejects.toThrow(/Internal Server Error/);
  });

  it("和审批裁决是两个端点，不会互相串", async () => {
    // 两者载荷没有交集（一个是裁决 + 可选参数修改，一个是一句话），后端的前置
    // 状态校验也不同（waiting_approval vs waiting_input）。合成一个带 mode 的
    // 端点的话，每个字段都得写"仅当 mode=X 时有效"。
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okResponse(streamOf('data: {"type":"done"}\n')));
    vi.stubGlobal("fetch", fetchMock);

    for await (const _ of client.answerClarification("run-1", "答案")) void _;
    for await (const _ of client.resumeRun("run-1", true, "")) void _;

    const urls = fetchMock.mock.calls.map((c) => c[0] as string);
    expect(urls[0]).toContain("/answer");
    expect(urls[1]).toContain("/resume");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      answer: "答案",
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      approved: true,
      note: "",
    });
  });
});

describe("改参数再批准", () => {
  let client: ApiClient;

  beforeEach(() => {
    localStorage.clear();
    client = new ApiClient("http://test.invalid");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("没编辑时请求体里不出现 editedArguments", async () => {
    // 传 null 和不传在后端等价（`dict | None`），但省掉它让请求体如实反映
    // "这是一次原样批准"——排查时能一眼看出用户到底动没动参数。
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okResponse(streamOf('data: {"type":"done"}\n')));
    vi.stubGlobal("fetch", fetchMock);

    for await (const _ of client.resumeRun("run-1", true, "")) void _;

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).toEqual({ approved: true, note: "" });
    expect("editedArguments" in body).toBe(false);
  });

  it("只把改过的键发出去", async () => {
    // **这是整条编辑链最要紧的一条。** 卡片上的值来自 build_preview，它把字符串
    // mask_markup 过、还截断到 800 字。把没改的键一起回传，等于用有损副本覆盖
    // 原文——一次"只改标题"会把两千字正文静默变成八百字的脱敏版本，而界面上
    // 完全看不出损失。后端按 {**原参数, **这里给的} 合并，所以少传是安全的。
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okResponse(streamOf('data: {"type":"done"}\n')));
    vi.stubGlobal("fetch", fetchMock);

    for await (const _ of client.resumeRun("run-1", true, "", undefined, {
      name: "Q3 复盘",
    })) {
      void _;
    }

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.editedArguments).toEqual({ name: "Q3 复盘" });
    expect(body.editedArguments).not.toHaveProperty("content");
  });

  it("编辑仍然走 /resume，不是另一个端点", async () => {
    // 后端刻意没把"改完执行"做成第三种裁决：它经过同一道闸门、同一套 schema
    // 校验、写同一批 approved_call_ids。做成独立端点会让下游每个 `if approved`
    // 的地方都得再想一次"编辑算不算同意"。
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okResponse(streamOf('data: {"type":"done"}\n')));
    vi.stubGlobal("fetch", fetchMock);

    for await (const _ of client.resumeRun("run-1", true, "", undefined, {
      name: "x",
    })) {
      void _;
    }

    expect(fetchMock.mock.calls[0][0]).toContain("/chats/runs/run-1/resume");
  });
});
