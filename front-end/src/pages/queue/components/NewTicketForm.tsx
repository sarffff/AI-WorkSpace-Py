import React, { useState } from "react";
import { Loader2 } from "lucide-react";
import { apiClient, isConflictResponse } from "@/shared/api/client";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import type { SubmitTicketResponse, TicketChannel } from "@/shared/types/api.types";

const CHANNELS: { value: TicketChannel; label: string }[] = [
  { value: "web_chat", label: "网页聊天" },
  { value: "email", label: "邮件" },
  { value: "app", label: "APP" },
  { value: "api", label: "接口转入" },
];

/**
 * 手动录入一张工单。
 *
 * 只列**默认白名单里那四个渠道**。企微与电话转写在枚举里存在，但入站适配器还没做，
 * 前端把选项摆出来只会得到一句 400 和一次困惑；那两种渠道等它们的适配器落地再出现。
 *
 * `external_ref` 是去重键，界面上叫"渠道侧消息 ID"：同一个客户把同一句话发两遍
 * （邮件重投、用户连点两次）不该产生两张工单，而这个字段就是后端判那件事的依据。
 */
export const NewTicketForm: React.FC<{
  onSubmitted: (result: SubmitTicketResponse) => void;
  onCancel: () => void;
}> = ({ onSubmitted, onCancel }) => {
  const toast = useToast();
  const [content, setContent] = useState("");
  const [channel, setChannel] = useState<TicketChannel>("web_chat");
  const [email, setEmail] = useState("");
  const [subject, setSubject] = useState("");
  const [externalRef, setExternalRef] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!content.trim()) {
      toast.error("客户原文不能为空——Agent 判意图、抽编号都从这一段来");
      return;
    }
    setBusy(true);
    try {
      const result = await apiClient.submitTicket({
        channel,
        content: content.trim(),
        subject: subject.trim() || undefined,
        customer_email: email.trim() || undefined,
        external_ref: externalRef.trim() || undefined,
      });
      if (isConflictResponse(result)) {
        toast.error(result.message ?? "工单能力未开启");
        return;
      }
      onSubmitted(result);
    } catch (e) {
      toast.error(toastMessageFrom(e, "录入失败"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      onSubmit={submit}
      className="card-surface rounded-2xl p-4 anim-fade-up"
      aria-label="手动录入工单"
    >
      <div className="flex flex-wrap gap-3 mb-3">
        <label className="flex flex-col gap-1.5 min-w-[130px]">
          <span className="label-eyebrow">渠道</span>
          <select
            className="input-field"
            value={channel}
            onChange={(event) => setChannel(event.target.value as TicketChannel)}
          >
            {CHANNELS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1.5 flex-1 min-w-[180px]">
          <span className="label-eyebrow">客户邮箱（用来认出是谁）</span>
          <input
            className="input-field"
            type="email"
            value={email}
            placeholder="zhang@corp.com"
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>

        <label className="flex flex-col gap-1.5 min-w-[160px]">
          <span className="label-eyebrow">渠道侧消息 ID（去重用）</span>
          <input
            className="input-field input-mono"
            value={externalRef}
            placeholder="可选"
            onChange={(e) => setExternalRef(e.target.value)}
          />
        </label>
      </div>

      <label className="flex flex-col gap-1.5 mb-3">
        <span className="label-eyebrow">标题（可留空，Agent 会自己归纳）</span>
        <input
          className="input-field"
          value={subject}
          placeholder="例：订单未发货，客户要求查询进度"
          onChange={(e) => setSubject(e.target.value)}
        />
      </label>

      <label className="flex flex-col gap-1.5">
        <span className="label-eyebrow">客户原文</span>
        <textarea
          className="input-field"
          rows={3}
          value={content}
          placeholder="照客户原话粘贴。订单号、金额这些事实由 Agent 从这一段里抽，不要替它改写。"
          onChange={(e) => setContent(e.target.value)}
        />
      </label>

      <div className="flex items-center justify-end gap-2 mt-3">
        <button type="button" onClick={onCancel} className="btn-quiet" disabled={busy}>
          取消
        </button>
        <button
          type="submit"
          className="btn-accent px-3.5 py-2 rounded-[10px] text-[12px] font-semibold"
          disabled={busy}
        >
          {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
          受理这张工单
        </button>
      </div>
    </form>
  );
};
