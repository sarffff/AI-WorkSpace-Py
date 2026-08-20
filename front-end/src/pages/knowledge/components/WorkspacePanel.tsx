import React from "react";
import type { WorkspaceInfo } from "@/shared/types/api.types";
import { useToast } from "@/shared/ui/Toast";
import { Users, KeyRound, RefreshCw } from "lucide-react";

export const WorkspacePanel: React.FC<{
    workspace: WorkspaceInfo | null;
    onRegenerate: () => void;
}> = ({ workspace, onRegenerate }) => {
    const toast = useToast();
    if (!workspace) return null;

    const copyInviteCode = async () => {
        if (!workspace.inviteCode) return;
        try {
            await navigator.clipboard.writeText(workspace.inviteCode);
            toast.success("邀请码已复制");
        } catch {
            toast.error("复制失败");
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
                            {workspace.role === "admin" ? "管理员" : "成员"}
                        </div>
                    </div>
                </div>

                {workspace.inviteCode && (
                    <div className="flex items-center gap-2">
                        <div className="flex items-center gap-2 bg-[#f3f0e6] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-3 py-1.5">
                            <KeyRound className="w-3.5 h-3.5 text-[#da7756]" />
                            <span
                                className="text-xs font-semibold tracking-widest text-[#1f1e1d] dark:text-[#edece8]"
                                style={{ fontFamily: "var(--font-mono)" }}
                            >
                                {workspace.inviteCode}
                            </span>
                            <button
                                onClick={copyInviteCode}
                                title="复制邀请码"
                                aria-label="复制邀请码"
                                className="p-1 rounded text-[#6e6b63] dark:text-[#a19f96] hover:text-[#da7756] transition-colors"
                            >
                                <svg
                                    className="w-3.5 h-3.5"
                                    viewBox="0 0 24 24"
                                    fill="none"
                                    stroke="currentColor"
                                    strokeWidth="2"
                                >
                                    <rect x="9" y="9" width="13" height="13" rx="2" />
                                    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                                </svg>
                            </button>
                        </div>
                        <button
                            onClick={onRegenerate}
                            title="重置邀请码（旧码立即作废）"
                            aria-label="重置邀请码"
                            className="p-2 rounded-xl text-[#6e6b63] dark:text-[#a19f96] hover:text-[#da7756] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] transition-colors"
                        >
                            <RefreshCw className="w-3.5 h-3.5" />
                        </button>
                    </div>
                )}
            </div>

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
