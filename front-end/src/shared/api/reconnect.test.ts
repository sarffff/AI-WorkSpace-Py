/**
 * 断线静默重连的测试。
 *
 * 挑的都是「实现里做过一个具体决定」的点，而不是把分支走一遍：
 *
 * - 只有「流没正常结束」才重连（done / error 都不重连）
 * - 悄悄结束（没抛异常但也没 done）也算断线——fetch 的 body 流断掉时不一定抛
 * - 接续时服务端会重发已经发过的前缀，不能让用户看两遍
 * - 接上又断掉要重新给满重试预算，否则长回答断三次会被误判成接不上
 * - NOT_RESUMABLE 不重试（断在工具中途，重跑可能重复写入）
 * - 没有 runId 无从接续
 */
import { describe, expect, it, vi } from "vitest";

import type { StreamChunk } from "@/shared/types/api.types";
import {
  MAX_SILENT_RETRIES,
  backoffMs,
  streamWithReconnect,
  type ReconnectChunk,
} from "./reconnect";

/** 把一批事件做成一条正常结束的流。 */
async function* streamOf(
  ...chunks: StreamChunk[]
): AsyncGenerator<StreamChunk, void, undefined> {
  for (const chunk of chunks) {
    yield chunk;
  }
}

/** 发几个事件然后抛异常（网络断掉）。 */
function throwingAfter(...chunks: StreamChunk[]) {
  return async function* (): AsyncGenerator<StreamChunk, void, undefined> {
    for (const chunk of chunks) {
      yield chunk;
    }
    throw new Error("network error");
  };
}

/** 发几个事件然后悄悄结束——没抛，也没 done。 */
function silentlyEndingAfter(...chunks: StreamChunk[]) {
  return async function* (): AsyncGenerator<StreamChunk, void, undefined> {
    for (const chunk of chunks) {
      yield chunk;
    }
  };
}

const started = (runId = "run-1"): StreamChunk => ({
  type: "run_started",
  runId,
});
const delta = (content: string): StreamChunk => ({
  type: "message_delta",
  content,
});
const done = (): StreamChunk => ({ type: "done", done: true });

async function collect(
  gen: AsyncGenerator<ReconnectChunk, void, undefined>,
): Promise<ReconnectChunk[]> {
  const out: ReconnectChunk[] = [];
  for await (const chunk of gen) {
    out.push(chunk);
  }
  return out;
}

const noSleep = () => Promise.resolve();

function textOf(chunks: ReconnectChunk[]): string {
  return chunks
    .filter((c): c is StreamChunk => c.type === "message_delta")
    .map((c) => c.content ?? "")
    .join("");
}

// ---- 不该重连的情形 ------------------------------------------------------

describe("正常结束不重连", () => {
  it("看到 done 就收工", async () => {
    const open = vi.fn(() => streamOf(started(), delta("你好"), done()));
    const resume = vi.fn(() => streamOf());

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep }),
    );

    expect(resume).not.toHaveBeenCalled();
    expect(textOf(chunks)).toBe("你好");
    expect(chunks.some((c) => c.type === "reconnecting")).toBe(false);
  });

  it("服务端明确报错时不重连", async () => {
    // 重连一个明确失败的请求只是把同一个错误再拿一遍——而且要再花一次钱。
    const open = vi.fn(() =>
      streamOf(started(), { type: "error", error: "模型调用失败" }),
    );
    const resume = vi.fn(() => streamOf());

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep }),
    );

    expect(resume).not.toHaveBeenCalled();
    expect(chunks.some((c) => c.type === "error")).toBe(true);
    expect(chunks.some((c) => c.type === "reconnecting")).toBe(false);
  });
});

// ---- 该重连的情形 --------------------------------------------------------

describe("断线重连", () => {
  it("抛异常之后静默接回去", async () => {
    const open = throwingAfter(started(), delta("前半"));
    const resume = vi.fn(() => streamOf(delta("后半"), done()));

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep }),
    );

    expect(resume).toHaveBeenCalledWith("run-1");
    expect(textOf(chunks)).toBe("前半后半");
    // 静默 = 有一条 reconnecting 供界面显示淡提示，但没有 reconnect_failed
    expect(chunks.filter((c) => c.type === "reconnecting")).toHaveLength(1);
    expect(chunks.some((c) => c.type === "reconnect_failed")).toBe(false);
  });

  it("悄悄结束也算断线", async () => {
    // fetch 的 body 流在网络断掉时不一定抛，可能只是 read() 返回 done:true。
    // 只靠 try/catch 会把这种情况当成正常结束，用户看到半截回答且没有任何提示。
    const open = silentlyEndingAfter(started(), delta("半截"));
    const resume = vi.fn(() => streamOf(delta("补齐"), done()));

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep }),
    );

    expect(resume).toHaveBeenCalledWith("run-1");
    expect(textOf(chunks)).toBe("半截补齐");
  });

  it("正文原样透出，不做去重", async () => {
    // 我先写了一段"跳过服务端重发前缀"的逻辑，核对服务端之后删掉了：
    // streamed_prefix 只在**落库**时用（让库里的回答和用户看到的一致），
    // 流本身是从断点之后接着发的，而且接续只从 post_tools 起——那一轮已经
    // 跑完，正文不会重来。
    //
    // 这条钉住"不去重"：带去重的实现会把恰好重复的正文悄悄吃掉，而中文回答里
    // 短句重复很常见（"是的。"、"共 3 条。"）。
    const open = throwingAfter(started(), delta("共 3 条。"));
    const resume = vi.fn(() => streamOf(delta("共 3 条。"), done()));

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep }),
    );

    expect(textOf(chunks)).toBe("共 3 条。共 3 条。");
  });
});

// ---- 重试预算 ------------------------------------------------------------

describe("重试次数", () => {
  it("试满次数之后交给用户", async () => {
    const open = throwingAfter(started(), delta("x"));
    const resume = vi.fn(() => {
      return (async function* (): AsyncGenerator<
        StreamChunk,
        void,
        undefined
      > {
        throw new Error("still down");
      })();
    });

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep, maxRetries: 3 }),
    );

    expect(resume).toHaveBeenCalledTimes(3);
    const failed = chunks.find((c) => c.type === "reconnect_failed");
    expect(failed).toBeDefined();
    expect(failed).toMatchObject({ attempts: 3, resumable: true });
  });

  it("接上又断掉要重新给满预算", async () => {
    // 一次长回答里断三次，每次都接上了并吐了新内容——那是"能接上"，
    // 不该在第三次就宣告失败。不清零的话长回答在弱网下永远走不完。
    let call = 0;
    const open = throwingAfter(started(), delta("1"));
    const resume = vi.fn(() => {
      call += 1;
      if (call <= 4) {
        return throwingAfter(delta(String(call + 1)))();
      }
      return streamOf(delta("末"), done());
    });

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep, maxRetries: 2 }),
    );

    // 5 次接续都发生了，尽管 maxRetries 只有 2——因为每次都有进展
    expect(resume).toHaveBeenCalledTimes(5);
    expect(chunks.some((c) => c.type === "reconnect_failed")).toBe(false);
    expect(textOf(chunks)).toBe("12345末");
  });

  it("一直接不上就不会无限重试", async () => {
    const open = throwingAfter(started());
    // 每次都立刻断且没有任何新内容 = 没有进展，预算正常消耗
    const resume = vi.fn(() => throwingAfter()());

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep, maxRetries: 2 }),
    );

    expect(resume).toHaveBeenCalledTimes(2);
    expect(chunks.some((c) => c.type === "reconnect_failed")).toBe(true);
  });
});

// ---- 接不上的情形 --------------------------------------------------------

describe("无法接续", () => {
  it("服务端说接不上就不重试", async () => {
    // 断在工具执行中途，重跑可能重复写入。这不是网络问题。
    const open = throwingAfter(started(), delta("x"));
    const resume = vi.fn(() => {
      return (async function* (): AsyncGenerator<
        StreamChunk,
        void,
        undefined
      > {
        throw new Error("NOT_RESUMABLE");
      })();
    });

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep, maxRetries: 3 }),
    );

    expect(resume).toHaveBeenCalledTimes(1);
    const failed = chunks.find((c) => c.type === "reconnect_failed");
    expect(failed).toMatchObject({ resumable: false });
  });

  it("没拿到 runId 时无从接续", async () => {
    // 第一次连接就断了，服务端没来得及发 run_started。
    const open = throwingAfter();
    const resume = vi.fn(() => streamOf());

    const chunks = await collect(
      streamWithReconnect({ open, resume, sleep: noSleep }),
    );

    expect(resume).not.toHaveBeenCalled();
    const failed = chunks.find((c) => c.type === "reconnect_failed");
    expect(failed).toMatchObject({ resumable: false });
  });
});

// ---- 中止 ----------------------------------------------------------------

describe("用户中止", () => {
  it("已中止时不再重连", async () => {
    const controller = new AbortController();
    controller.abort();
    const open = throwingAfter(started(), delta("x"));
    const resume = vi.fn(() => streamOf());

    await collect(
      streamWithReconnect({
        open,
        resume,
        sleep: noSleep,
        signal: controller.signal,
      }),
    );

    expect(resume).not.toHaveBeenCalled();
  });
});

// ---- 退避 ----------------------------------------------------------------

describe("退避间隔", () => {
  it("指数增长但有上限", async () => {
    // 断线往往成群出现（切换网络、服务端重启），固定间隔会让一批客户端
    // 同步重试；没有上限则第 10 次要等十几分钟。
    expect(backoffMs(1)).toBeLessThan(backoffMs(2));
    expect(backoffMs(2)).toBeLessThan(backoffMs(3));
    expect(backoffMs(20)).toBeLessThanOrEqual(5000);
  });

  it("默认静默次数是个小数字", async () => {
    // 每次接续都是真实的模型调用，要花钱。静默太多次等于悄悄烧钱。
    expect(MAX_SILENT_RETRIES).toBeLessThanOrEqual(5);
  });
});
