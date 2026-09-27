import React, { useState } from "react";
import { HelpCircle, Send } from "lucide-react";

/**
 * 澄清卡片：模型调了 ask_user，停在这里等一句回答。
 *
 * 和 ToolApprovalCard 是**两种不同的中断**，所以是两个组件：
 * - 审批：模型想做某件事，等人裁决「做不做」。有默认答案（不做）。
 * - 澄清：模型缺一个它猜不出来的事实，等人给「是什么」。没有默认答案。
 *
 * 于是这张卡片没有"跳过"按钮：跳过一个 ask_user 等于让模型拿着空答案继续，
 * 那比停在这里更糟——它会自己编一个。要中止就中止整个回合。
 *
 * `resumable=false` 时（没开 checkpoint）走的是旧行为：答案作为新一轮消息发出去，
 * 模型这一轮检索到的东西全部丢掉。这个差别对用户是可见的（模型会重新查一遍），
 * 所以卡片上要说清楚，而不是静默降级。
 *
 * `adopted=true` 时这句问题是**框架从回答正文里收编来的**（模型没调 ask_user，
 * 见后端 chat_service._maybe_adopt_prose_question）。区别只在文案：那句问题在
 * 上面的回答里已经出现过一次了，标题再写"需要你补充一句"会让人觉得被问了两遍。
 */

interface ClarificationCardProps {
  question: string;
  /**
   * 能不能接着原来那一轮继续。false 时答案变成新消息，
   * 模型问问题之前检索到的一切都会丢掉。
   */
  resumable: boolean;
  /**
   * 这句问题是不是从回答正文里收编来的。true 时问题已经在上面的回答里出现过，
   * 所以卡片不再重复它，只给输入框。
   */
  adopted?: boolean;
  /** 提交进行中：禁用输入与按钮，否则会送出两次回答 */
  busy?: boolean;
  onAnswer: (answer: string) => void;
}

export const ClarificationCard: React.FC<ClarificationCardProps> = ({
  question,
  resumable,
  adopted = false,
  busy = false,
  onAnswer,
}) => {
  const [answer, setAnswer] = useState("");
  const trimmed = answer.trim();
  // 后端 ClarificationAnswerRequest 是 min_length=1 / max_length=10_000。
  // 空答案在这里就拦掉，不去换一个 422。
  const canSend = trimmed.length > 0 && !busy;

  const submit = () => {
    if (!canSend) return;
    onAnswer(trimmed);
  };

  return (
    <div className="my-3 rounded-xl border border-sky-500/40 bg-sky-500/5 dark:bg-sky-500/[0.07] overflow-hidden">
      <div className="flex items-start gap-2.5 px-4 pt-3.5 pb-2.5">
        <HelpCircle className="w-4 h-4 mt-0.5 shrink-0 text-sky-600 dark:text-sky-400" />
        <div className="min-w-0">
          <div className="text-sm font-medium text-[#3d3929] dark:text-[#e8e6dc]">
            {adopted ? "在这里回答就能接着上面那一步" : "需要你补充一句"}
          </div>
          {/* 收编来的问题不再重复渲染：它就在上面那段回答的末尾，
              再显示一遍会让人觉得被问了两遍。 */}
          {!adopted ? (
            <div className="mt-1 text-xs leading-relaxed whitespace-pre-wrap break-words text-[#3d3929] dark:text-[#e8e6dc]">
              {question}
            </div>
          ) : null}
          {!resumable ? (
            <div className="mt-1.5 text-[10px] leading-relaxed text-[#a19f96]">
              这次回答会作为新的一条消息发出，模型会重新检索一遍
              （没有开启执行快照）
            </div>
          ) : null}
        </div>
      </div>

      <div className="px-4 pb-3">
        <textarea
          autoFocus
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          onKeyDown={(e) => {
            // Enter 发送、Shift+Enter 换行：和主输入框一致。
            // 澄清答案通常只有几个字，每次都要去点按钮很别扭。
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          disabled={busy}
          maxLength={10000}
          rows={2}
          placeholder="回答之后模型会接着刚才那一步继续"
          className="w-full resize-none rounded-lg px-3 py-2 text-xs bg-[#faf9f5] dark:bg-[#1f1e1c] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#3d3929] dark:text-[#e8e6dc] placeholder:text-[#a19f96] focus:outline-none focus:border-sky-500/60 disabled:opacity-50"
        />
      </div>

      <div className="flex items-center justify-end gap-2 px-4 pb-3.5">
        <button
          type="button"
          disabled={!canSend}
          onClick={submit}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-sky-600 hover:bg-sky-700 text-white disabled:opacity-50"
        >
          <Send className="w-3.5 h-3.5" />
          {busy ? "继续中..." : "回答"}
        </button>
      </div>
    </div>
  );
};
