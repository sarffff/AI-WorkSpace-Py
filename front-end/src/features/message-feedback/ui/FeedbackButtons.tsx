import React, { useState } from "react";
import { ThumbsUp, ThumbsDown, Check, X } from "lucide-react";
import { apiClient } from "@/shared/api/client";
import type {
  FeedbackReason,
  MessageFeedback,
} from "@/shared/types/api.types";

const REASON_LABELS: { value: FeedbackReason; label: string }[] = [
  { value: "inaccurate", label: "内容不准确" },
  { value: "no_citation", label: "没引用来源" },
  { value: "off_topic", label: "答非所问" },
  { value: "bad_format", label: "格式或语言问题" },
  { value: "other", label: "其它" },
];

interface Props {
  /** 服务端消息 id。流式回答要等 done 事件回写后才有，此前按钮不可用 */
  messageId?: string;
  initial?: MessageFeedback;
}

/**
 * 单条回答的赞/踩。
 *
 * 点踩会展开原因与"应该怎么答"——后者是关键：只有一句期望答案，才能把这次差评
 * 变成离线回归用例（见 back-end/eval/from_feedback.py）。光有踩只是个计数器。
 */
export const FeedbackButtons: React.FC<Props> = ({ messageId, initial }) => {
  const [rating, setRating] = useState<"up" | "down" | null>(
    initial?.rating ?? null,
  );
  const [expanded, setExpanded] = useState(false);
  const [reason, setReason] = useState<FeedbackReason>(
    (initial?.reason as FeedbackReason) ?? "inaccurate",
  );
  const [expectedAnswer, setExpectedAnswer] = useState(
    initial?.expectedAnswer ?? "",
  );
  const [comment, setComment] = useState(initial?.comment ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 流式期间还没有服务端 id，此时提交会 404
  const disabled = !messageId || busy;

  const send = async (
    next: "up" | "down",
    extra?: { reason?: FeedbackReason; comment?: string; expectedAnswer?: string },
  ) => {
    if (!messageId) return;
    setBusy(true);
    setError(null);
    try {
      if (rating === next && !extra) {
        // 再次点击同一个按钮 = 撤销
        await apiClient.revokeFeedback(messageId);
        setRating(null);
        setExpanded(false);
        return;
      }
      await apiClient.submitFeedback({
        messageId,
        rating: next,
        ...extra,
      });
      setRating(next);
      if (next === "up") setExpanded(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败");
    } finally {
      setBusy(false);
    }
  };

  const handleDown = () => {
    if (rating === "down") {
      setExpanded((prev) => !prev);
      return;
    }
    setExpanded(true);
    void send("down");
  };

  const submitDetails = () =>
    send("down", {
      reason,
      comment: comment.trim() || undefined,
      expectedAnswer: expectedAnswer.trim() || undefined,
    }).then(() => setExpanded(false));

  return (
    <div className="inline-flex flex-col gap-1.5">
      <div className="inline-flex items-center gap-1">
        <button
          onClick={() => void send("up")}
          disabled={disabled}
          title={messageId ? "回答有帮助" : "等回答结束后可评价"}
          aria-label="评价回答有帮助"
          className={`p-1.5 rounded-lg transition-colors disabled:opacity-40 ${
            rating === "up"
              ? "text-emerald-600 bg-emerald-500/10"
              : "hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#e6e2d8]/60 dark:hover:bg-[#282724]/60"
          }`}
        >
          <ThumbsUp className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={handleDown}
          disabled={disabled}
          title={messageId ? "回答有问题" : "等回答结束后可评价"}
          aria-label="评价回答有问题"
          className={`p-1.5 rounded-lg transition-colors disabled:opacity-40 ${
            rating === "down"
              ? "text-rose-600 bg-rose-500/10"
              : "hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#e6e2d8]/60 dark:hover:bg-[#282724]/60"
          }`}
        >
          <ThumbsDown className="w-3.5 h-3.5" />
        </button>
      </div>

      {expanded && rating === "down" && (
        <div className="w-80 rounded-xl border border-[#e6e2d8] dark:border-[#282724] bg-white dark:bg-[#1a1917] p-3 space-y-2 shadow-sm">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold text-[#1f1e1d] dark:text-[#edece8]">
              哪里不对？
            </span>
            <button
              onClick={() => setExpanded(false)}
              aria-label="收起反馈面板"
              className="text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
          <div className="flex flex-wrap gap-1">
            {REASON_LABELS.map((item) => (
              <button
                key={item.value}
                onClick={() => setReason(item.value)}
                className={`px-2 py-1 rounded-lg text-[10px] transition-colors ${
                  reason === item.value
                    ? "bg-[#da7756] text-white"
                    : "bg-[#f3f0e6] dark:bg-[#201f1c] text-[#6e6b63] dark:text-[#a19f96]"
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>
          <textarea
            value={expectedAnswer}
            onChange={(e) => setExpectedAnswer(e.target.value)}
            rows={3}
            placeholder="应该怎么答？（填了才能变成回归用例）"
            className="w-full resize-none rounded-lg bg-[#f3f0e6] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] px-2 py-1.5 text-[11px] text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] focus:outline-none"
          />
          <input
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="补充说明（可选）"
            className="w-full rounded-lg bg-[#f3f0e6] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] px-2 py-1.5 text-[11px] text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] focus:outline-none"
          />
          {error && <div className="text-[10px] text-rose-600">{error}</div>}
          <button
            onClick={() => void submitDetails()}
            disabled={busy}
            className="w-full px-2 py-1.5 rounded-lg bg-[#da7756] hover:bg-[#c86544] disabled:opacity-60 text-white text-[11px] font-medium flex items-center justify-center gap-1.5"
          >
            <Check className="w-3 h-3" />
            保存
          </button>
        </div>
      )}
    </div>
  );
};
