import React, { useCallback, useEffect, useState } from "react";
import { Copy, KeyRound, LogIn, Users, Loader2 } from "lucide-react";
import { apiClient } from "@/shared/api/client";
import { PageHeader } from "@/shared/ui/PageHeader";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import { MemberPanel } from "../components/MemberPanel";
import type { WorkspaceInfo } from "@/shared/types/api.types";

/**
 * 工作区：成员、邀请码、换空间。
 *
 * 原来这三件事散在"设置"页的不同标签里，和主题、模型偏好混在一起。它们不是偏好：
 * 一条决定谁能看见共享文档，一条决定谁能进来看。工单台上尤其要紧——
 * 一个客服组换人时如果找不到"把上一家公司的人移出去"在哪，共享知识库就一直漏着。
 */
export const WorkspacePage: React.FC = () => {
  const toast = useToast();
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [invite, setInvite] = useState("");

  const load = useCallback(async () => {
    try {
      setWorkspace(await apiClient.getWorkspace());
    } catch (e) {
      toast.error(toastMessageFrom(e, "工作区信息读取失败"));
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const regenerate = async () => {
    setBusy("invite");
    try {
      const result = await apiClient.regenerateInviteCode();
      setWorkspace((previous) =>
        previous ? { ...previous, inviteCode: result.inviteCode } : previous
      );
      toast.success("邀请码已重置，旧码立刻作废");
    } catch (e) {
      toast.error(toastMessageFrom(e, "重置失败"));
    } finally {
      setBusy(null);
    }
  };

  const join = async () => {
    if (!invite.trim()) {
      toast.error("先填一个邀请码");
      return;
    }
    setBusy("join");
    try {
      const result = await apiClient.joinWorkspace(invite.trim());
      setWorkspace(result.workspace);
      setInvite("");
      // 加入是**换空间**，不是多一个空间。不提醒的话，人会发现"我的资料不见了"
      toast.info(
        result.leftBehindDocuments > 0
          ? `已进入 ${result.workspace.name}。原空间里你上传的 ${result.leftBehindDocuments} 篇文档不再参与检索——它们没被删，但你在这儿看不到。`
          : `已进入 ${result.workspace.name}`
      );
    } catch (e) {
      toast.error(toastMessageFrom(e, "加入失败"));
    } finally {
      setBusy(null);
    }
  };

  const isAdmin = Boolean(workspace?.isAdmin);

  return (
    <div className="page-shell">
      <div className="max-w-[860px] mx-auto flex flex-col gap-5">
        <PageHeader
          description="共享知识库、SOP 与工单的边界。谁能进、谁管得着组织资产，都在这页处理。"
        />

        <section className="card-surface rounded-2xl p-5">
          <div className="flex items-center gap-2 mb-3">
            <KeyRound className="w-4 h-4 text-accent" />
            <h2 className="text-[14px] font-semibold text-ink">邀请码</h2>
            <span className="text-[11px] text-ink-faint ml-auto">
              {isAdmin ? "仅管理员可见与重置" : "你不在这个空间的管理名单里"}
            </span>
          </div>

          {isAdmin ? (
            <div className="flex items-center gap-2">
              <code className="num flex-1 px-3 py-2 rounded-lg text-[13px] tracking-wider" style={{ background: "var(--hl-code-bg)", color: "var(--hl-code-ink)" }}>
                {workspace?.inviteCode ?? "—"}
              </code>
              <button
                type="button"
                className="btn-quiet"
                onClick={() => {
                  if (!workspace?.inviteCode) return;
                  navigator.clipboard
                    ?.writeText(workspace.inviteCode)
                    .then(() => toast.info("邀请码已复制"))
                    .catch(() => toast.error("浏览器不允许读取剪贴板，请手动抄写"));
                }}
                aria-label="复制邀请码"
              >
                <Copy className="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                className="btn-quiet"
                disabled={busy !== null}
                onClick={() => void regenerate()}
              >
                {busy === "invite" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
                重置
              </button>
            </div>
          ) : (
            <p className="text-[12px] text-ink-faint">
              邀请码只发给管理员。需要新人进来，请在成员名单里把他提为管理员。
            </p>
          )}

          <div className="flex items-center gap-2 mt-4 pt-4 border-t border-line">
            <input
              className="input-field input-mono flex-1"
              placeholder="用别人的邀请码加入另一个空间"
              value={invite}
              onChange={(event) => setInvite(event.target.value)}
            />
            <button
              type="button"
              className="btn-accent px-3.5 py-2 rounded-[10px] text-[12px] font-semibold"
              disabled={busy !== null}
              onClick={() => void join()}
            >
              <LogIn className="w-3.5 h-3.5" />
              换到那个空间
            </button>
          </div>
          <p className="text-[11px] text-ink-faint mt-2 leading-relaxed">
            加入新空间会**离开**当前空间：你自己上传的文档留在原处，但在新空间里既不显示也不参与检索。
            这不是删除，回来就能看到——不过当时人会以为丢了。
          </p>
        </section>

        <section>
          <div className="flex items-center gap-2 mb-2">
            <Users className="w-4 h-4 text-accent" />
            <h2 className="text-[14px] font-semibold text-ink">成员</h2>
          </div>
          <MemberPanel />
        </section>
      </div>
    </div>
  );
};
