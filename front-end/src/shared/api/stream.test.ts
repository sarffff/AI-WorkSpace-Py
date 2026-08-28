import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { ApiClient } from "./client";

/**
 * SSE 流解析。这是前端风险最高的一段未测代码：整个对话界面的内容都从这里来，
 * 而它的失效方式全是"安静地少一点东西"——丢一个事件、截断一个汉字、
 * 把一行没读完的 JSON 当成坏数据跳过。这些都不会抛异常。
 *
 * 测的是公开入口 `streamMessage`，不是私有的 `readStream`：私有方法要靠
 * 类型断言掏出来，那样测的就不是真实调用路径了。
 */

/** 把若干段字符串包装成一个可读流，模拟服务端分块发送。 */
const streamOf = (...chunks: string[]): ReadableStream<Uint8Array> => {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
};

/** 按字节切分，用来造"多字节字符被切断"的场景。 */
const byteChunks = (text: string, size: number): Uint8Array[] => {
  const all = new TextEncoder().encode(text);
  const out: Uint8Array[] = [];
  for (let i = 0; i < all.length; i += size) {
    out.push(all.slice(i, i + size));
  }
  return out;
};

const rawStreamOf = (parts: Uint8Array[]): ReadableStream<Uint8Array> =>
  new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(part);
      controller.close();
    },
  });

const okResponse = (body: ReadableStream<Uint8Array>) =>
  ({ ok: true, status: 200, statusText: "OK", body }) as unknown as Response;

const collect = async (client: ApiClient) => {
  const out: unknown[] = [];
  for await (const chunk of client.streamMessage({ message: "q" } as never)) {
    out.push(chunk);
  }
  return out;
};

describe("SSE 流解析", () => {
  let client: ApiClient;

  beforeEach(() => {
    localStorage.clear();
    client = new ApiClient("http://test.invalid");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("逐行解析出每个事件", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        okResponse(
          streamOf(
            'data: {"type":"message_delta","content":"你"}\n',
            'data: {"type":"message_delta","content":"好"}\n',
            'data: {"type":"done"}\n',
          ),
        ),
      ),
    );

    const chunks = await collect(client);
    expect(chunks).toEqual([
      { type: "message_delta", content: "你" },
      { type: "message_delta", content: "好" },
      { type: "done" },
    ]);
  });

  it("一个事件被拆到两个网络块里也能解析", async () => {
    // 真实链路上 TCP 分段与事件边界毫无关系。缓冲区拼接写错的话，
    // 这里会丢事件而不是报错。
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        okResponse(
          streamOf('data: {"type":"message_de', 'lta","content":"合并"}\n'),
        ),
      ),
    );

    expect(await collect(client)).toEqual([
      { type: "message_delta", content: "合并" },
    ]);
  });

  it("多个事件挤在一个块里全部解析出来", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        okResponse(
          streamOf(
            'data: {"type":"a"}\ndata: {"type":"b"}\ndata: {"type":"c"}\n',
          ),
        ),
      ),
    );

    expect(await collect(client)).toEqual([
      { type: "a" },
      { type: "b" },
      { type: "c" },
    ]);
  });

  it("汉字被按字节切断时不会变成乱码", async () => {
    // 这是最容易漏的一条：UTF-8 里一个汉字 3 字节，按字节切流会把它劈开。
    // TextDecoder 必须带 {stream:true} 才会把残字节留到下一次；漏了这个参数
    // 的症状是内容里出现 U+FFFD，而且只在中文和 emoji 上出现。
    const payload = 'data: {"type":"message_delta","content":"中文内容测试"}\n';
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(okResponse(rawStreamOf(byteChunks(payload, 5)))),
    );

    const chunks = await collect(client);
    expect(chunks).toEqual([
      { type: "message_delta", content: "中文内容测试" },
    ]);
    expect(JSON.stringify(chunks)).not.toContain("�");
  });

  it("emoji（4 字节）被切断同样不受影响", async () => {
    const payload = 'data: {"type":"message_delta","content":"🎉完成"}\n';
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(okResponse(rawStreamOf(byteChunks(payload, 3)))),
    );

    expect(await collect(client)).toEqual([
      { type: "message_delta", content: "🎉完成" },
    ]);
  });

  it("跳过非 data: 行（注释与心跳）而不中断", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        okResponse(
          streamOf(
            ": keep-alive\n",
            "event: ping\n",
            "\n",
            'data: {"type":"real"}\n',
          ),
        ),
      ),
    );

    expect(await collect(client)).toEqual([{ type: "real" }]);
  });

  it("坏 JSON 被跳过，其后的事件仍然送达", async () => {
    // 容错方向是对的（一行坏了不该让整个回答断掉），但它意味着解析失败是
    // 静默的。这条测试的作用是把"静默"限定在坏行本身。
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        okResponse(
          streamOf(
            'data: {"type":"before"}\n',
            "data: {不是合法 JSON\n",
            'data: {"type":"after"}\n',
          ),
        ),
      ),
    );

    expect(await collect(client)).toEqual([
      { type: "before" },
      { type: "after" },
    ]);
  });

  it("data: 后面为空的行被忽略", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        okResponse(streamOf("data:\n", "data:   \n", 'data: {"type":"x"}\n')),
      ),
    );

    expect(await collect(client)).toEqual([{ type: "x" }]);
  });

  it("最后一行没有换行结尾时会被丢掉（当前行为）", async () => {
    // 缓冲区里剩下的残行在流结束后没有被 flush。服务端总是以 \n 结尾，
    // 所以现在不出问题；钉住它是为了让将来有人改协议时立刻看见这个假设。
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        okResponse(
          streamOf('data: {"type":"kept"}\n', 'data: {"type":"lost"}'),
        ),
      ),
    );

    expect(await collect(client)).toEqual([{ type: "kept" }]);
  });

  it("HTTP 非 2xx 时抛错而不是静默返回空流", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        statusText: "Internal Server Error",
        body: null,
      } as unknown as Response),
    );

    await expect(collect(client)).rejects.toThrow(/Internal Server Error/);
  });

  it("响应体为 null 时抛错", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        statusText: "OK",
        body: null,
      } as unknown as Response),
    );

    await expect(collect(client)).rejects.toThrow(/Response body is null/);
  });
});

describe("流式请求的 401 重试", () => {
  let client: ApiClient;

  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem("access_token", "expired-token");
    localStorage.setItem("refresh_token", "refresh-token");
    client = new ApiClient("http://test.invalid");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("401 时先刷新 token，再用新 token 重发", async () => {
    const fetchMock = vi
      .fn()
      // 第一次流请求：过期
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        statusText: "Unauthorized",
        body: null,
      } as unknown as Response)
      // 刷新
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          access_token: "fresh-token",
          refresh_token: "refresh-token",
        }),
      } as unknown as Response)
      // 重发成功
      .mockResolvedValueOnce(okResponse(streamOf('data: {"type":"ok"}\n')));
    vi.stubGlobal("fetch", fetchMock);

    expect(await collect(client)).toEqual([{ type: "ok" }]);

    // 重发那次必须带新 token。不断言这一条的话，"刷新成功但仍用旧头重发"
    // 这个 bug 会完全隐形（表现为偶发的对话失败）。
    const retry = fetchMock.mock.calls[2];
    const headers = (retry[1] as RequestInit).headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer fresh-token");
  });

  it("没有 refresh token 时不尝试刷新，直接抛错", async () => {
    localStorage.removeItem("refresh_token");
    const bare = new ApiClient("http://test.invalid");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
      body: null,
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);

    await expect(collect(bare)).rejects.toThrow(/Unauthorized/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("刷新失败时抛出原始 401，不静默返回空流", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        statusText: "Unauthorized",
        body: null,
      } as unknown as Response)
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        statusText: "Unauthorized",
      } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);

    await expect(collect(client)).rejects.toThrow(/Unauthorized/);
  });
});
