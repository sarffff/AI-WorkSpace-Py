/**
 * 断线静默重连。
 *
 * 包在 `streamMessage` 外面：断线时自己接回去，接不上再告诉用户。对 `ChatPage`
 * 来说它和原来那个生成器长得一样，所以消费端不用改结构。
 *
 * ## 为什么能接回去
 *
 * 服务端在轮次边界（`post_tools`）落快照，并且 SSE 的 finally 会把断掉的回合标成
 * `interrupted`。所以接续 = 从最后一个完成的轮次继续发下一轮模型调用，**一次工具
 * 都不会重跑**。断在工具执行中途的服务端会直接拒绝（409），因为那种情况下重跑
 * 可能重复写入——那时我们不重试，交给用户重新提问。
 *
 * ## 判据：什么算断线
 *
 * 只有**流没有正常结束**才算。`done` 事件到了就是成功，`error` 事件是服务端明确
 * 报错（模型失败、限流），两者都不重连——重连一个明确失败的请求只是把同一个错误
 * 再拿一遍。真正要接的是"迭代抛异常"或"流悄悄结束了但没有 done"。
 *
 * 后者容易漏：`fetch` 的 body 流在网络断掉时不一定抛，可能只是 `read()` 返回
 * `done: true`。所以这里跟踪 `sawDone`，而不是只靠 try/catch。
 *
 * ## 为什么先静默、几次之后才问
 *
 * 一次 WiFi 抖动接回去就好了，弹窗只是噪音。但无限静默重试更糟：用户看着光标闪
 * 却不知道发生了什么，而每次接续都是真实的模型调用（要花钱）。所以给一个明确的
 * 次数上限，用完之后把决定权交回去。
 *
 * 退避是指数的但有上限：断线往往成群出现（切换网络、服务端重启），固定间隔会让
 * 一批客户端同步重试。
 */
import type { StreamChunk } from "@/shared/types/api.types";

/** 静默重连几次之后才询问用户。 */
export const MAX_SILENT_RETRIES = 3;

/** 退避基数（毫秒）。实际间隔是 base * 2^(n-1)，上限 BACKOFF_CAP_MS。 */
const BACKOFF_BASE_MS = 600;
const BACKOFF_CAP_MS = 5000;

export function backoffMs(attempt: number): number {
  const raw = BACKOFF_BASE_MS * 2 ** Math.max(0, attempt - 1);
  return Math.min(raw, BACKOFF_CAP_MS);
}

/** 重连过程中额外发给消费端的事件。类型故意与 StreamChunk 兼容。 */
export type ReconnectNotice =
  | {
      type: "reconnecting";
      /** 第几次尝试，从 1 开始 */
      attempt: number;
      /** 一共会静默试几次 */
      maxAttempts: number;
    }
  | {
      type: "reconnect_failed";
      /** 试了几次 */
      attempts: number;
      /** 还能不能接（false = 服务端说这个回合接不上了，只能重新提问） */
      resumable: boolean;
    };

export type ReconnectChunk = StreamChunk | ReconnectNotice;

export interface ReconnectOptions {
  /** 开一条新流（第一次调用）。 */
  open: () => AsyncGenerator<StreamChunk, void, undefined>;
  /** 用 runId 接上一条断掉的流。 */
  resume: (runId: string) => AsyncGenerator<StreamChunk, void, undefined>;
  maxRetries?: number;
  /** 退避等待。可注入以便测试不用真的睡。 */
  sleep?: (ms: number) => Promise<void>;
  signal?: AbortSignal;
}

const defaultSleep = (ms: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * 带静默重连的流。
 *
 * 正常情况下它逐个透出底层事件。断线时先发 `reconnecting`（界面可以显示一行
 * 淡提示，但不该弹窗），接上了就继续；试满次数还不行才发 `reconnect_failed`。
 */
export async function* streamWithReconnect(
  options: ReconnectOptions,
): AsyncGenerator<ReconnectChunk, void, undefined> {
  const maxRetries = options.maxRetries ?? MAX_SILENT_RETRIES;
  const sleep = options.sleep ?? defaultSleep;

  let runId: string | null = null;
  let attempt = 0;
  let resumable = true;

  while (true) {
    let sawDone = false;
    let sawError = false;
    // 本次尝试是否真的产出了新内容。用于判断"接上了"——接上之后要把重试
    // 计数清零，否则一次长回答里断三次就会误判成"接不上"。
    let progressed = false;

    // 显式标注：不写的话 TS 会把这里的类型推导绕回生成器自身的 yield 类型，
    // 报 TS7022（circularly references itself）。
    const stream: AsyncGenerator<StreamChunk, void, undefined> =
      runId === null ? options.open() : options.resume(runId);

    try {
      for await (const chunk of stream) {
        if (options.signal?.aborted) {
          return;
        }
        if (chunk.type === "run_started" && chunk.runId) {
          runId = chunk.runId;
        }
        // 这里**不做**正文去重。
        //
        // 我先前写了一段"跳过服务端重发的前缀"的逻辑，然后去核对服务端才发现
        // 它不会重发：``streamed_prefix`` 只在**落库**时用（让库里的回答和用户
        // 看到的一致），流本身是从断点之后接着发的。而且接续只从 post_tools
        // 开始——那一轮已经跑完，它的正文不会再来一遍。
        //
        // 留着那段逻辑更糟：它会把"恰好和先前内容重复的正文"当成回放悄悄吃掉。
        if (chunk.type === "error" || chunk.error) {
          sawError = true;
        }
        if (chunk.type === "done" || chunk.done) {
          sawDone = true;
        }
        progressed = true;
        yield chunk;
      }
    } catch (error) {
      if (options.signal?.aborted) {
        return;
      }
      // 服务端明确说这个回合接不上（断在工具执行中途，重跑可能重复写入）。
      // 这不是网络问题，重试多少次都一样。
      if (error instanceof Error && error.message === "NOT_RESUMABLE") {
        resumable = false;
        sawDone = false;
      }
    }

    if (sawDone || sawError) {
      // 正常结束，或服务端明确报错。两者都不该重连。
      return;
    }
    if (options.signal?.aborted) {
      return;
    }

    // 走到这里 = 流没有正常结束。
    if (progressed) {
      // 这次接上了并且吐了新内容，只是又断了。重试预算重新给满——
      // 不清零的话一次长回答里断三次就会被当成"接不上"。
      attempt = 0;
    }

    if (runId === null || !resumable || attempt >= maxRetries) {
      // 没有 runId 就无从接续（第一次连接就失败，或服务端没来得及发
      // run_started）；resumable 为假是服务端说了不行；次数用完则交回用户。
      yield {
        type: "reconnect_failed",
        attempts: attempt,
        resumable: resumable && runId !== null,
      };
      return;
    }

    attempt += 1;
    yield { type: "reconnecting", attempt, maxAttempts: maxRetries };
    await sleep(backoffMs(attempt));
    if (options.signal?.aborted) {
      return;
    }
  }
}
