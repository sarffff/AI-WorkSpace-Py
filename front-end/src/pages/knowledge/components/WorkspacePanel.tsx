import React, { useState } from "react";
import { apiClient } from "@/shared/api/client";
import type { WorkspaceInfo } from "@/shared/types/api.types";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import { Check, Copy, LogIn, RefreshCw, Users } from "lucide-react";

/**
 * 工作区面板：归属、成员、邀请码、加入。
 *
 * 三条界面上的规矩，都对应后端的一条规则：
 *
 * 1. **邀请码只给 admin 看。** 后端 ``workspace_info`` 对 user 返回 null，
 *    所以这里是"拿不到就不显示"，而不是"拿到了但藏起来"。
 * 2. **加入是换空间，不是多一个空间**（``User.workspace_id`` 是单值外键）。
 *    所以加入前必须确认，并把原空间会失去访问的文档数说清楚——静默切换
 *    会让人以为自己的资料丢了。
 * 3. **重置邀请码立即作废旧码**，这是泄露后的止损动作，所以也要确认。
 */
export const WorkspacePanel: React.FC<{
    workspace: WorkspaceInfo | null;
    onChanged?: () => void;
}> = ({ workspace, onChanged }) => {
    const toast = useToast();
    const [joining, setJoining] = useState(false);
    const [code, setCode] = useState("");
    const [copied, setCopied] = useState(false);
    const [showJoin, setShowJoin] = useState(false);

    if (!workspace) return null;

    // 权限判据用后端算好的 isAdmin：存量账号的 role 还可能是历史值 "member"
    const isAdmin = workspace.isAdmin ?? workspace.role === "admin";

    const handleCopy = async () => {
        if (!workspace.inviteCode) return;
        try {
            await navigator.clipboard.writeText(workspace.inviteCode);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch {
            toast.error("复制失败，请手动选中复制");
        }
    };

    const handleRegenerate = async () => {
        if (
            !window.confirm(
                "重置后旧邀请码立即失效，已加入的成员不受影响。确定重置？",
            )
        )
            return;
        try {
            await apiClient.regenerateInviteCode();
            toast.success("邀请码已重置，旧码立即失效");
            onChanged?.();
        } catch (err) {
            toast.error(toastMessageFrom(err, "重置邀请码失败"));
        }
    };

    const handleJoin = async () => {
        const trimmed = code.trim();
        if (!trimmed) return;
        setJoining(true);
        try {
            const result = await apiClient.joinWorkspace(trimmed);
            // 先提示"会失去什么"再提示成功：加入是换空间，原空间的文档
            // 不会被删但再也检索不到，这一点必须说出来
            if (result.leftBehindDocuments > 0) {
                toast.info(
                    `已加入。原空间的 ${result.leftBehindDocuments} 份文档仍然保留，` +
                        "但不会再出现在检索结果里",
                );
            } else {
                toast.success(`已加入「${result.workspace.name}」`);
            }
            setCode("");
            setShowJoin(false);
            onChanged?.();
        } catch (err) {
            // 后端的 detail 是面向用户的文案（"邀请码无效"、"你已在该工作区中"）
            toast.error(toastMessageFrom(err, "加入工作区失败"));
        } finally {
            setJoining(false);
        }
    };

    return (
        <div className="card-surface rounded-2xl p-5 anim-fade-up">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex items-center gap-2.5 min-w-0">
                    <div className="w-9 h-9 rounded-xl bg-[#da7756]/10 text-[#da7756] flex items-center justify-center shrink-0">
                        <Users className="w-4 h-4" />
                    </div>
                    <div className="min-w-0">
                        <div className="text-xs font-medium text-[#1f1e1d] dark:text-[#edece8] truncate">
                            {workspace.name}
                        </div>
                        <div className="text-[10px] text-[#918d83]">
                            {workspace.memberCount} 名成员共享此知识库 · 你是
                            {isAdmin ? "管理员" : "普通成员"}
                            {isAdmin ? "" : "（可上传个人文档）"}
                        </div>
                    </div>
                </div>

                <div className="flex items-center gap-2 shrink-0">
                    {/* 邀请码：后端只发给 admin，拿不到就不渲染 */}
                    {workspace.inviteCode && (
                        <>
                            <button
                                onClick={handleCopy}
                                title="复制邀请码，发给要加入的同事"
                                className="px-2.5 py-1.5 rounded-xl bg-[#f3f0e6] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[11px] font-mono tracking-wider text-[#1f1e1d] dark:text-[#edece8] flex items-center gap-1.5 hover:border-[#da7756]/40 transition-colors"
                            >
                                {workspace.inviteCode}
                                {copied ? (
                                    <Check className="w-3 h-3 text-emerald-600" />
                                ) : (
                                    <Copy className="w-3 h-3 opacity-60" />
                                )}
                            </button>
                            <button
                                onClick={handleRegenerate}
                                title="重置邀请码（旧码立即失效）"
                                aria-label="重置邀请码"
                                className="p-1.5 rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:text-[#da7756] hover:bg-[#da7756]/10 transition-colors"
                            >
                                <RefreshCw className="w-3.5 h-3.5" />
                            </button>
                        </>
                    )}
                    <button
                        onClick={() => setShowJoin((v) => !v)}
                        className="px-2.5 py-1.5 rounded-xl text-[11px] text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#f3f0e6] dark:hover:bg-[#22211e] transition-colors flex items-center gap-1.5"
                    >
                        <LogIn className="w-3.5 h-3.5" />
                        加入其他工作区
                    </button>
                </div>
            </div>

            {showJoin && (
                <div className="mt-3 pt-3 border-t border-[#e6e2d8]/60 dark:border-[#282724]/60">
                    <div className="flex items-center gap-2">
                        <input
                            value={code}
                            onChange={(e) => setCode(e.target.value.toUpperCase())}
                            onKeyDown={(e) => {
                                if (e.key === "Enter") handleJoin();
                            }}
                            placeholder="输入 8 位邀请码"
                            maxLength={16}
                            className="flex-1 px-3 py-2 rounded-xl bg-[#f3f0e6] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] text-xs font-mono tracking-wider text-[#1f1e1d] dark:text-[#edece8] placeholder:text-[#918d83] focus:outline-none focus:border-[#da7756]"
                        />
                        <button
                            onClick={handleJoin}
                            disabled={joining || !code.trim()}
                            className="btn-accent px-4 py-2 text-white text-xs font-medium rounded-xl disabled:opacity-50"
                        >
                            {joining ? "加入中..." : "加入"}
                        </button>
                    </div>
                    {/* 这句话不是装饰：单值外键决定了加入就是离开，
                        不说清楚的话用户会以为原来的文档丢了 */}
                    <p className="mt-2 text-[10px] text-[#918d83] leading-relaxed">
                        加入后你将离开当前工作区，成为新工作区的普通成员。
                        原工作区的文档不会被删除，但不再出现在你的检索结果里。
                    </p>
                </div>
            )}

            {workspace.members.length > 1 && (
                <div className="flex flex-wrap gap-1.5 mt-3 pt-3 border-t border-[#e6e2d8]/60 dark:border-[#282724]/60">
                    {workspace.members.map((m) => (
                        <span
                            key={m.id}
                            className={`px-2 py-0.5 rounded-lg text-[10px] ${
                                m.role === "admin"
                                    ? "bg-[#da7756]/10 text-[#da7756]"
                                    : "bg-[#f3f0e6] dark:bg-[#201f1c] text-[#6e6b63] dark:text-[#a19f96]"
                            }`}
                        >
                            {m.name}
                            {m.role === "admin" ? " · 管理员" : ""}
                        </span>
                    ))}
                </div>
            )}
        </div>
    );
};
