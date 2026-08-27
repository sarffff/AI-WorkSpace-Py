import React, { useCallback, useEffect, useState } from "react";
import { apiClient } from "@/shared/api/client";
import type { UserMemory } from "@/shared/types/api.types";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import { Brain, RefreshCw, Trash2 } from "lucide-react";

/**
 * 长期记忆管理。
 *
 * 后端在每轮回答结束后用辅助模型从对话里抽取事实与偏好（MEMORY_ENABLED），
 * 注入之后所有会话的系统上下文。这里给用户两件事：看见系统记住了什么、
 * 删掉不该记的。删除是即时生效的——下一轮就不再注入，不需要重启或清缓存。
 */
const KIND_META: Record<
    string,
    { label: string; className: string }
> = {
    preference: {
        label: "偏好",
        className:
            "bg-[#da7756]/12 text-[#da7756]",
    },
    fact: {
        label: "事实",
        className:
            "bg-[#e3dfd5] dark:bg-[#2e2d2a] text-[#6e6b63] dark:text-[#a19f96]",
    },
};

const fmtTime = (iso: string) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const diffMs = Date.now() - d.getTime();
    const day = 86_400_000;
    if (diffMs < 60_000) return "刚刚";
    if (diffMs < 3_600_000) return `${Math.floor(diffMs / 60_000)} 分钟前`;
    if (diffMs < day) return `${Math.floor(diffMs / 3_600_000)} 小时前`;
    if (diffMs < 30 * day) return `${Math.floor(diffMs / day)} 天前`;
    return d.toLocaleDateString();
};

export const MemoryPanel: React.FC<{ className?: string }> = ({
    className = "",
}) => {
    const toast = useToast();
    const [memories, setMemories] = useState<UserMemory[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    // 删除中的条目本地置灰，等响应回来移除——连删多条时不互相阻塞
    const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());

    const refresh = useCallback(() => {
        apiClient
            .getMemories()
            .then(setMemories)
            .catch((e) =>
                setError(e instanceof Error ? e.message : "加载记忆失败"),
            );
    }, []);

    useEffect(() => {
        refresh();
    }, [refresh]);

    const handleDelete = async (memory: UserMemory) => {
        setDeletingIds((prev) => new Set(prev).add(memory.id));
        try {
            await apiClient.deleteMemory(memory.id);
            setMemories((prev) =>
                prev ? prev.filter((m) => m.id !== memory.id) : prev,
            );
            toast.success("已删除，之后的对话不再注入这条记忆");
        } catch (e) {
            toast.error(toastMessageFrom(e, "删除失败"));
        } finally {
            setDeletingIds((prev) => {
                const next = new Set(prev);
                next.delete(memory.id);
                return next;
            });
        }
    };

    return (
        <div className={`card-surface p-6 rounded-2xl space-y-4 ${className}`}>
            <div className="flex items-center gap-2.5 pb-3 border-b border-[#e6e2d8] dark:border-[#282724]">
                <span className="w-6 h-6 rounded-lg bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
                    <Brain className="w-3.5 h-3.5" />
                </span>
                <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                        <h4 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                            长期记忆
                        </h4>
                        {memories && memories.length > 0 && (
                            <span className="chip text-[10px] px-1.5 py-0.5">
                                {memories.length} 条
                            </span>
                        )}
                    </div>
                    <p className="text-[11px] text-[#918d83] mt-0.5">
                        对话中聊到的事实与偏好会被自动抽取，注入之后的每一次会话。删除立即生效。
                    </p>
                </div>
                {memories && memories.length > 0 && (
                    <button
                        onClick={refresh}
                        title="刷新"
                        aria-label="刷新长期记忆"
                        className="p-1.5 rounded-lg text-[#918d83] hover:text-[#da7756] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] transition-colors"
                    >
                        <RefreshCw className="w-3.5 h-3.5" />
                    </button>
                )}
            </div>

            {error && (
                <p className="text-xs text-rose-600 dark:text-rose-400">{error}</p>
            )}

            {!memories && !error && (
                <div className="space-y-2" aria-hidden>
                    {[0, 1].map((i) => (
                        <div
                            key={i}
                            className="h-9 rounded-xl bg-[#f3f0e6] dark:bg-[#201f1c] animate-pulse"
                            style={{ width: `${88 - i * 18}%` }}
                        />
                    ))}
                </div>
            )}

            {memories && memories.length === 0 && !error && (
                <div className="rounded-xl border border-dashed border-[#e3dfd5] dark:border-[#2e2d2a] p-6 text-center">
                    <Brain className="w-5 h-5 text-[#c9c4b6] dark:text-[#33312d] mx-auto mb-2" />
                    <p className="text-xs text-[#6e6b63] dark:text-[#a19f96] leading-relaxed max-w-sm mx-auto">
                        还没有记住任何东西。像「我习惯用 Python」「我们团队周五开周会」
                        这样的话聊过之后，就会出现在这里——新会话开场即知。
                    </p>
                </div>
            )}

            {memories && memories.length > 0 && (
                <ul className="divide-y divide-[#e6e2d8]/60 dark:divide-[#282724]/60 -my-1">
                    {memories.map((m, i) => {
                        const kind =
                            KIND_META[m.kind] ?? {
                                label: m.kind,
                                className:
                                    "bg-[#e3dfd5] dark:bg-[#2e2d2a] text-[#6e6b63]",
                            };
                        const deleting = deletingIds.has(m.id);
                        return (
                            <li
                                key={m.id}
                                className="group flex items-start gap-3 py-2.5 anim-fade-up"
                                style={{ animationDelay: `${Math.min(i, 8) * 0.04}s` }}
                            >
                                <span
                                    className={`shrink-0 mt-0.5 px-1.5 py-0.5 rounded-md text-[10px] font-semibold ${kind.className}`}
                                >
                                    {kind.label}
                                </span>
                                <p
                                    className={`flex-1 min-w-0 text-xs leading-relaxed text-[#1f1e1d] dark:text-[#edece8] break-words transition-opacity ${
                                        deleting ? "opacity-40 line-through" : ""
                                    }`}
                                >
                                    {m.content}
                                </p>
                                <span className="shrink-0 mt-0.5 text-[10px] text-[#918d83] whitespace-nowrap">
                                    {fmtTime(m.createdAt)}
                                </span>
                                <button
                                    onClick={() => handleDelete(m)}
                                    disabled={deleting}
                                    title="删除这条记忆"
                                    aria-label="删除这条记忆"
                                    className="shrink-0 mt-0.5 p-1 rounded-md text-[#c9c4b6] dark:text-[#4a4843] opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-rose-500 hover:bg-rose-500/10 disabled:cursor-not-allowed transition-all"
                                >
                                    <Trash2 className="w-3.5 h-3.5" />
                                </button>
                            </li>
                        );
                    })}
                </ul>
            )}
        </div>
    );
};
