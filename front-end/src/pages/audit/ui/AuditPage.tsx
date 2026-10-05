import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CheckCircle2, Fingerprint, Link2, Loader2 } from "lucide-react";
import { apiClient } from "@/shared/api/client";
import { fmtDateTime } from "@/shared/lib/format";
import { EmptyState } from "@/shared/ui/EmptyState";
import { PageHeader } from "@/shared/ui/PageHeader";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import type { AuditEntry, AuditVerifyResponse } from "@/shared/types/api.types";

/**
 * 审计链。
 *
 * 这一页存在的意义是那个 `verify`：一条 append-only 的哈希链如果只能"看"，
 * 它就只是一份日志；能校验才回答得出"这串记录有没有被改过"。
 * 所以校验结果常驻页首，而不是收在一个按钮后面。
 */
export const AuditPage: React.FC = () => {
  const navigate = useNavigate();
  const toast = useToast();
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [verify, setVerify] = useState<AuditVerifyResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [list, chain] = await Promise.all([
        apiClient.getAuditEntries(100, 0),
        apiClient.verifyAuditChain(),
      ]);
      setEntries(list.entries);
      setVerify(chain);
    } catch (e) {
      toast.error(toastMessageFrom(e, "审计记录读取失败"));
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="page-shell">
      <div className="max-w-[900px] mx-auto flex flex-col gap-4">
        <PageHeader
          description="谁在什么时候让系统做了什么。参数只存摘要与截断预览：原文可能含客户 PII。"
          actions={
            <button type="button" className="btn-quiet" onClick={() => void load()}>
              刷新
            </button>
          }
        />

        {verify && (
          <div
            className="rounded-2xl px-4 py-3 flex items-center gap-2 text-[12.5px]"
            style={{
              background: verify.ok ? "var(--c-done-faint)" : "var(--c-bad-faint)",
              border: `1px solid color-mix(in srgb, var(--c-${verify.ok ? "done" : "bad"}) 32%, transparent)`,
              color: verify.ok ? "var(--c-done)" : "var(--c-bad)",
            }}
            role="status"
          >
            {verify.ok ? (
              <CheckCircle2 className="w-4 h-4 shrink-0" />
            ) : (
              <Link2 className="w-4 h-4 shrink-0" />
            )}
            <span className="font-semibold">
              {verify.ok ? "哈希链完整" : `链在第 ${verify.firstBrokenSeq} 条对不上`}
            </span>
            <span className="num ml-auto text-ink-faint">{verify.entries} 条已校验</span>
          </div>
        )}

        <div className="card-surface rounded-2xl overflow-hidden">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-14 text-ink-soft text-sm">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在读取…
            </div>
          ) : entries.length === 0 ? (
            <div className="py-14">
              <EmptyState
                icon={<Fingerprint className="w-7 h-7 text-accent" />}
                title="还没有审计记录"
                description="处置工单、暂停 Agent、抑制消息这类动作都会留下一条。"
              />
            </div>
          ) : (
            entries.map((entry) => (
              <div key={entry.id} className="ledger-row px-4 py-3 gap-3 items-start">
                <span className="trace-gutter w-8 shrink-0 pt-0.5">{entry.seq}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="trace-node">{entry.action}</span>
                    {entry.target && (
                      <span className="text-[11.5px] text-ink-soft truncate max-w-[46ch]">
                        {entry.target}
                      </span>
                    )}
                    <span className="num text-[10.5px] text-ink-faint ml-auto shrink-0">
                      {fmtDateTime(entry.createdAt)}
                    </span>
                  </div>
                  {entry.argumentsPreview && (
                    <p className="num text-[11px] text-ink-faint mt-1 break-all line-clamp-2">
                      {entry.argumentsPreview}
                    </p>
                  )}
                  <div className="flex items-center gap-2 mt-1">
                    {entry.argumentsDigest && (
                      <span className="num text-[10px] text-ink-faint">
                        #{entry.argumentsDigest.slice(0, 12)}
                      </span>
                    )}
                    {entry.ticketId && (
                      <button
                        type="button"
                        onClick={() => navigate(`/tickets/${entry.ticketId}`)}
                        className="text-[10.5px] text-accent hover:underline"
                      >
                        看那张工单
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
