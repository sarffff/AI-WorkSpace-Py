import React, { useState } from "react";
import { Check, ShieldAlert, X } from "lucide-react";
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
  onDecide: (approved: boolean, note: string) => void;
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

const FIELD_LABELS: Record<string, string> = {
  title: "标题",
  content: "正文",
  document_id: "文档 ID",
  filename: "文件名",
  tags: "标签",
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

  const all = Object.entries(preview ?? {});
  const entries = all.filter(([key]) => !key.endsWith(CHARS_SUFFIX));
  /** 键 -> 原文字符数，只有被截断的字段才有 */
  const truncated = new Map<string, number>(
    all
      .filter(([key, value]) => key.endsWith(CHARS_SUFFIX) && typeof value === "number")
      .map(([key, value]) => [key.slice(0, -CHARS_SUFFIX.length), value as number]),
  );

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
          {entries.map(([key, value]) => (
            <div key={key} className="px-3 py-2 flex gap-3 text-xs">
              <span className="shrink-0 w-16 text-[#6e6b63] dark:text-[#a19f96]">
                {FIELD_LABELS[key] ?? key}
              </span>
              <span className="min-w-0 whitespace-pre-wrap break-words text-[#3d3929] dark:text-[#e8e6dc]">
                {renderValue(value)}
                {truncated.has(key) ? (
                  <span className="block mt-1 text-[10px] text-[#a19f96]">
                    原文共 {truncated.get(key)!.toLocaleString()} 字，此处只显示开头一段；
                    同意后写入的是完整内容
                  </span>
                ) : null}
              </span>
            </div>
          ))}
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
            <button
              type="button"
              disabled={busy}
              onClick={() => onDecide(true, "")}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white disabled:opacity-50"
            >
              <Check className="w-3.5 h-3.5" />
              {busy ? "执行中..." : "同意执行"}
            </button>
          </>
        )}
      </div>
    </div>
  );
};
