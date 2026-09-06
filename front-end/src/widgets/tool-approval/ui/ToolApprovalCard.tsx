import React, { useState } from "react";
import { Check, Pencil, ShieldAlert, X } from "lucide-react";
import { toolLabel } from "@/shared/lib/format";

/**
 * 工具审批卡片：模型想执行一个写操作，停在这里等人点。
 *
 * 这是可恢复执行唯一"看得见"的地方。后端在调用写工具之前把整个回合存进
 * agent_checkpoints，然后结束这条 SSE；用户点同意或拒绝之后走
 * POST /chats/runs/{runId}/resume 从快照接着跑。所以这张卡片不依赖任何
 * 活着的连接——刷新页面之后由 GET /chats/runs/pending 重新拉回来。
 *
 * 拒绝要能写理由，并且理由会作为工具结果回给模型。只说"不行"的话，模型下一轮
 * 很可能换个参数再试一次同样的写操作；说了"知识库里已经有了"它才会改变计划。
 */

interface ToolApprovalCardProps {
  tool: string;
  /** 批准之后会发生什么（后端 approval._REASONS 给的，不是前端猜的） */
  reason?: string;
  /** 参数预览，已在后端做过 mask_markup */
  preview: Record<string, unknown>;
  /** 恢复请求进行中：两个按钮都要禁用，否则会发出两次裁决 */
  busy?: boolean;
  /**
   * `edits` 只包含**用户真正改过的键**，没改的不传。
   *
   * 这一点是硬要求，不是优化：这张卡片显示的值来自后端 `build_preview`，它对
   * 字符串做了两件有损的事（`mask_markup` 中和标记语法，再截断到 800 字）。
   * 把没改过的键一起回传，等于用有损副本覆盖原值——一次"只改标题"的编辑会把
   * 两千字正文静默变成八百字的脱敏版本。
   */
  onDecide: (
    approved: boolean,
    note: string,
    edits?: Record<string, unknown>,
  ) => void;
}

/** 预览值渲染成一行；对象/数组落到 JSON，长文本截断 */
const renderValue = (value: unknown): string => {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

/**
 * 后端对超长字符串会截断，并额外给一个 `<键>__chars` 记原文长度
 * （services/approval.py 的 build_preview）。
 *
 * 它不是一个参数，不能当条目渲染——那会变成一行「content__chars 2000」。
 * 但也不能丢掉：卡片上显示的是前 800 字，用户点「同意」批准的是完整 2000 字，
 * 这个差额必须让人看见，否则审批看到的和实际执行的不是同一个东西。
 */
const CHARS_SUFFIX = "__chars";

/**
 * `__` 前缀的键不是参数，是后端算好的展示补充（services/fs_tools.preview_extra）。
 *
 * 它们必须和真实参数分开渲染：`__diff` 的值是一个字符串数组，当成参数渲染会变成
 * 一行 JSON；而它恰恰是这张卡片上最该被读到的东西——文件操作的参数写着"把
 * old_text 换成 new_text"，那看不出这次改动到底动了什么。
 */
const EXTRA_PREFIX = "__";

const FIELD_LABELS: Record<string, string> = {
  title: "标题",
  content: "正文",
  document_id: "文档 ID",
  filename: "文件名",
  tags: "标签",
  path: "文件",
  old_text: "原内容",
  new_text: "新内容",
  reason: "原因",
};

/** 一行 diff 按前缀上色。上下文行不上色，否则整块都是颜色，加减就不显眼了 */
const diffLineClass = (line: string): string => {
  if (line.startsWith("+++") || line.startsWith("---")) {
    return "text-[#a19f96]";
  }
  if (line.startsWith("+")) {
    return "text-emerald-700 dark:text-emerald-400 bg-emerald-500/10";
  }
  if (line.startsWith("-")) {
    return "text-rose-700 dark:text-rose-400 bg-rose-500/10";
  }
  if (line.startsWith("@@")) {
    return "text-[#da7756]";
  }
  return "text-[#6e6b63] dark:text-[#a19f96]";
};

export const ToolApprovalCard: React.FC<ToolApprovalCardProps> = ({
  tool,
  reason,
  preview,
  busy = false,
  onDecide,
}) => {
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  /**
   * 编辑态。键 -> 用户输入的新值，只装**动过的**字段。
   *
   * 用"只装动过的"而不是"进编辑态就把 preview 整个拷进来"：后者会让所有字段都
   * 变成"改过的"，于是全部被回传，截断字段的有损副本也跟着覆盖回去。
   */
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState(false);

  const all = Object.entries(preview ?? {});
  const entries = all.filter(
    ([key]) => !key.endsWith(CHARS_SUFFIX) && !key.startsWith(EXTRA_PREFIX),
  );
  /** 文件操作的改动预览。后端算的——前端没有文件访问权 */
  const diff = Array.isArray(preview?.__diff)
    ? (preview.__diff as string[])
    : null;
  const diffTruncated = preview?.__diff_truncated === true;
  /** 新建文件时没有 diff，只有行数：那时用户判断的是"这个文件该不该存在" */
  const newFileLines =
    preview?.__new_file === true && typeof preview.__lines === "number"
      ? preview.__lines
      : null;
  /** 键 -> 原文字符数，只有被截断的字段才有 */
  const truncated = new Map<string, number>(
    all
      .filter(([key, value]) => key.endsWith(CHARS_SUFFIX) && typeof value === "number")
      .map(([key, value]) => [key.slice(0, -CHARS_SUFFIX.length), value as number]),
  );

  /** 能在卡片里改的字段：字符串、且没被截断 */
  const editableKeys = entries
    .filter(([key, value]) => typeof value === "string" && !truncated.has(key))
    .map(([key]) => key);

  /**
   * 真正要回传的编辑：只有**值确实变了**的键。
   *
   * 光看 `key in edits` 不够——点进输入框再改回原样也会留下一条记录，
   * 那样会白触发一次"参数被改过"的说明回灌给模型，而实际什么都没变。
   * 一个都没变时返回 `undefined`，让调用方走原样批准那条路。
   */
  const changedEdits = (() => {
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(edits)) {
      if (value !== preview[key]) out[key] = value;
    }
    return Object.keys(out).length > 0 ? out : undefined;
  })();

  return (
    <div className="my-3 rounded-xl border border-amber-500/40 bg-amber-500/5 dark:bg-amber-500/[0.07] overflow-hidden">
      <div className="flex items-start gap-2.5 px-4 pt-3.5 pb-2.5">
        <ShieldAlert className="w-4 h-4 mt-0.5 shrink-0 text-amber-600 dark:text-amber-400" />
        <div className="min-w-0">
          <div className="text-sm font-medium text-[#3d3929] dark:text-[#e8e6dc]">
            需要确认：{toolLabel(tool)}
          </div>
          {reason ? (
            <div className="mt-0.5 text-xs leading-relaxed text-[#6e6b63] dark:text-[#a19f96]">
              {reason}
            </div>
          ) : null}
        </div>
      </div>

      {entries.length ? (
        <div className="mx-4 mb-3 rounded-lg bg-[#faf9f5] dark:bg-[#1f1e1c] border border-[#e3dfd5] dark:border-[#2e2d2a] divide-y divide-[#e3dfd5] dark:divide-[#2e2d2a]">
          {entries.map(([key, value]) => {
            // 被截断的字段**不给编辑**。卡片上只有开头 800 字，让人在这上面改再
            // 提交，等于用有损副本覆盖原文——而界面上完全看不出损失。
            // 要改长正文得让模型重写（拒绝并说明），那条路不会丢东西。
            const editable = editing && !truncated.has(key) && typeof value === "string";
            const dirty = key in edits;
            return (
              <div key={key} className="px-3 py-2 flex gap-3 text-xs">
                <span className="shrink-0 w-16 text-[#6e6b63] dark:text-[#a19f96]">
                  {FIELD_LABELS[key] ?? key}
                </span>
                {editable ? (
                  <span className="min-w-0 flex-1">
                    <input
                      value={dirty ? edits[key] : (value as string)}
                      onChange={(e) =>
                        setEdits((prev) => ({ ...prev, [key]: e.target.value }))
                      }
                      disabled={busy}
                      className="w-full rounded px-2 py-1 text-xs bg-white dark:bg-[#151412] border border-amber-500/40 text-[#3d3929] dark:text-[#e8e6dc] focus:outline-none focus:border-amber-500"
                    />
                    {dirty && edits[key] !== value ? (
                      <span className="block mt-1 text-[10px] text-amber-600 dark:text-amber-400">
                        已改动，原值：{renderValue(value)}
                      </span>
                    ) : null}
                  </span>
                ) : (
                  <span className="min-w-0 whitespace-pre-wrap break-words text-[#3d3929] dark:text-[#e8e6dc]">
                    {renderValue(dirty ? edits[key] : value)}
                    {truncated.has(key) ? (
                      <span className="block mt-1 text-[10px] text-[#a19f96]">
                        原文共 {truncated.get(key)!.toLocaleString()} 字，此处只显示开头一段；
                        同意后写入的是完整内容
                        {editing ? "。太长，不能在这里改——要改请拒绝并说明" : ""}
                      </span>
                    ) : null}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      ) : null}

      {/*
        文件操作的改动预览。放在参数表**下面**：先让人看清动的是哪个文件，
        再看具体改了什么。
      */}
      {diff ? (
        <div className="mx-4 mb-3 rounded-lg overflow-hidden border border-[#e3dfd5] dark:border-[#2e2d2a]">
          <div className="px-3 py-1.5 text-[11px] text-[#6e6b63] dark:text-[#a19f96] bg-[#faf9f5] dark:bg-[#1f1e1c] border-b border-[#e3dfd5] dark:border-[#2e2d2a]">
            这次会做的改动
            {diffTruncated ? "（只显示开头一段）" : ""}
          </div>
          {/*
            等宽字体 + 不换行：diff 靠列对齐读，折行之后加减号会跑到行中间，
            比截断更难读。横向滚动是对的。
          */}
          <pre className="max-h-64 overflow-auto px-3 py-2 text-[11px] leading-relaxed font-mono bg-white dark:bg-[#151412]">
            {diff.map((line, index) => (
              <div key={index} className={diffLineClass(line)}>
                {line || " "}
              </div>
            ))}
          </pre>
        </div>
      ) : null}

      {newFileLines !== null ? (
        <div className="mx-4 mb-3 rounded-lg border border-[#e3dfd5] dark:border-[#2e2d2a] bg-[#faf9f5] dark:bg-[#1f1e1c] px-3 py-2 text-[11px] text-[#6e6b63] dark:text-[#a19f96]">
          这是一个新文件，共 {newFileLines} 行。
        </div>
      ) : null}

      {rejecting ? (
        <div className="px-4 pb-3">
          <textarea
            autoFocus
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={2000}
            rows={2}
            placeholder="为什么不执行？这句话会作为工具结果回给模型，它据此调整下一步"
            className="w-full resize-none rounded-lg px-3 py-2 text-xs bg-[#faf9f5] dark:bg-[#1f1e1c] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#3d3929] dark:text-[#e8e6dc] placeholder:text-[#a19f96] focus:outline-none focus:border-amber-500/60"
          />
        </div>
      ) : null}

      <div className="flex items-center justify-end gap-2 px-4 pb-3.5">
        {rejecting ? (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setRejecting(false);
                setNote("");
              }}
              className="px-3 py-1.5 text-xs rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#e3dfd5]/60 dark:hover:bg-[#2e2d2a] disabled:opacity-50"
            >
              返回
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onDecide(false, note.trim())}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-rose-600 hover:bg-rose-700 text-white disabled:opacity-50"
            >
              <X className="w-3.5 h-3.5" />
              确认不执行
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => setRejecting(true)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg text-[#6e6b63] dark:text-[#a19f96] border border-[#e3dfd5] dark:border-[#2e2d2a] hover:bg-[#e3dfd5]/60 dark:hover:bg-[#2e2d2a] disabled:opacity-50"
            >
              <X className="w-3.5 h-3.5" />
              不执行
            </button>
            {/*
              编辑入口只在有可改字段时出现。全是截断的长文本时给一个编辑按钮，
              点进去发现什么都改不了，比没有这个按钮更糟。
            */}
            {!editing && editableKeys.length > 0 ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => setEditing(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg text-[#6e6b63] dark:text-[#a19f96] border border-[#e3dfd5] dark:border-[#2e2d2a] hover:bg-[#e3dfd5]/60 dark:hover:bg-[#2e2d2a] disabled:opacity-50"
              >
                <Pencil className="w-3.5 h-3.5" />
                改一下
              </button>
            ) : null}
            {editing ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setEditing(false);
                  setEdits({});
                }}
                className="px-3 py-1.5 text-xs rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#e3dfd5]/60 dark:hover:bg-[#2e2d2a] disabled:opacity-50"
              >
                撤销改动
              </button>
            ) : null}
            <button
              type="button"
              disabled={busy}
              onClick={() => onDecide(true, "", changedEdits)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50"
            >
              <Check className="w-3.5 h-3.5" />
              {busy
                ? "执行中..."
                : changedEdits
                  ? "按改动执行"
                  : "同意执行"}
            </button>
          </>
        )}
      </div>
    </div>
  );
};
