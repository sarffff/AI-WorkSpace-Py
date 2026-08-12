import React, { useEffect, useMemo, useState } from "react";
import { useDispatch } from "react-redux";
import { useNavigate } from "react-router-dom";
import { apiClient } from "@/shared/api/client";
import type { Prompt } from "@/shared/types/api.types";
import { setPendingInput, setCurrentChat } from "@/entities/chat/model/chatSlice";
import {
    Sparkles,
    Trash2,
    Pencil,
    Plus,
    X,
    Loader2,
    Terminal,
    Copy,
    Search,
} from "lucide-react";

type EditingState = {
    id: string | null; // null = 新建
    title: string;
    description: string;
    category: string;
    content: string;
    isPublic: boolean;
};

const EMPTY_EDITING: EditingState = {
    id: null,
    title: "",
    description: "",
    category: "通用",
    content: "",
    isPublic: true,
};

const CATEGORY_LABELS: Record<string, string> = {
    General: "通用",
    Engineering: "工程开发",
    Architecture: "架构设计",
    Database: "数据库",
    Writing: "写作",
};

const formatCategory = (category: string) => CATEGORY_LABELS[category] ?? category;

export const PromptsPage: React.FC = () => {
    const dispatch = useDispatch();
    const navigate = useNavigate();
    const [prompts, setPrompts] = useState<Prompt[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [searchQuery, setSearchQuery] = useState("");
    const [editing, setEditing] = useState<EditingState | null>(null);
    const [saving, setSaving] = useState(false);
    const [busyId, setBusyId] = useState<string | null>(null);

    const refresh = () => {
        setLoading(true);
        setError(null);
        apiClient
            .getPrompts()
            .then(setPrompts)
            .catch((e) => setError(e instanceof Error ? e.message : "加载失败"))
            .finally(() => setLoading(false));
    };

    useEffect(() => {
        refresh();
    }, []);

    const filtered = useMemo(() => {
        const q = searchQuery.trim().toLowerCase();
        if (!q) return prompts;
        return prompts.filter(
            (p) =>
                p.title.toLowerCase().includes(q) ||
                p.category.toLowerCase().includes(q) ||
                formatCategory(p.category).toLowerCase().includes(q) ||
                (p.description ?? "").toLowerCase().includes(q),
        );
    }, [prompts, searchQuery]);

    const handleUse = (p: Prompt) => {
        // 用模板 content 作为输入框预填（{input} 占位由用户自行替换）
        const draft = p.content.includes("{input}")
            ? p.content.replace("{input}", "")
            : p.content;
        dispatch(setCurrentChat(null));
        dispatch(setPendingInput(draft));
        navigate("/chat");
    };

    const handleCopy = async (p: Prompt) => {
        try {
            await navigator.clipboard.writeText(p.content);
        } catch {
            // 忽略
        }
    };

    const handleSave = async () => {
        if (!editing) return;
        if (!editing.title.trim() || !editing.content.trim()) {
            setError("标题和内容不能为空");
            return;
        }
        setSaving(true);
        setError(null);
        try {
            const body = {
                title: editing.title.trim(),
                description: editing.description.trim() || undefined,
                category: editing.category.trim() || "通用",
                content: editing.content,
                isPublic: editing.isPublic,
            };
            if (editing.id) {
                await apiClient.updatePrompt(editing.id, body);
            } else {
                await apiClient.createPrompt(body);
            }
            setEditing(null);
            refresh();
        } catch (e) {
            setError(e instanceof Error ? e.message : "保存失败");
        } finally {
            setSaving(false);
        }
    };

    const handleDelete = async (id: string) => {
        if (!window.confirm("确定删除该提示词模板？")) return;
        setBusyId(id);
        try {
            await apiClient.deletePrompt(id);
            refresh();
        } catch (e) {
            setError(e instanceof Error ? e.message : "删除失败");
        } finally {
            setBusyId(null);
        }
    };

    return (
        <div className="p-8 h-full overflow-y-auto space-y-6 bg-[#fbf9f5] dark:bg-[#141413] transition-colors duration-200">
            <div className="flex items-center justify-between">
                <div>
                    <h3 className="text-lg font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                        提示词工作台
                    </h3>
                    <p className="text-xs text-[#6e6b63] dark:text-[#a19f96] mt-1">
                        管理系统提示词模板，一键应用到对话。
                    </p>
                </div>
                <button
                    onClick={() => setEditing({ ...EMPTY_EDITING })}
                    className="px-4 py-2.5 bg-[#da7756] hover:bg-[#c86544] text-white text-xs font-medium rounded-xl flex items-center gap-2 shadow-md shadow-[#da7756]/20 transition-all"
                >
                    <Plus className="w-4 h-4" />
                    新建提示词
                </button>
            </div>

            {error && (
                <div className="flex items-center justify-between gap-3 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs">
                    <span>{error}</span>
                    <button
                        onClick={() => setError(null)}
                        className="p-0.5 hover:bg-rose-500/10 rounded"
                    >
                        <X className="w-3.5 h-3.5" />
                    </button>
                </div>
            )}

            <div className="flex items-center gap-2 bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] px-3.5 py-2 rounded-xl w-80 shadow-sm">
                <Search className="w-4 h-4 text-[#918d83]" />
                <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="搜索标题/分类/描述..."
                    className="bg-transparent border-none text-xs text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] focus:outline-none w-full"
                />
                {searchQuery && (
                    <button
                        onClick={() => setSearchQuery("")}
                        className="text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
                    >
                        <X className="w-3.5 h-3.5" />
                    </button>
                )}
            </div>

            {loading ? (
                <div className="flex justify-center py-16 text-[#918d83]">
                    <Loader2 className="w-6 h-6 animate-spin" />
                </div>
            ) : filtered.length === 0 ? (
                <div className="p-12 text-center text-xs text-[#918d83] bg-white dark:bg-[#1a1917] rounded-2xl border border-[#e6e2d8] dark:border-[#282724]">
                    {searchQuery ? "未找到匹配的提示词。" : "暂无提示词，点击右上角创建。"}
                </div>
            ) : (
                <div className="grid grid-cols-3 gap-6">
                    {filtered.map((p) => (
                        <div
                            key={p.id}
                            className="p-6 rounded-2xl bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] space-y-4 flex flex-col justify-between hover:border-[#da7756] transition-all group shadow-sm"
                        >
                            <div className="space-y-3">
                                <div className="flex items-center justify-between">
                                    <span className="text-[10px] px-2.5 py-1 rounded-full bg-[#da7756]/10 text-[#da7756] font-semibold uppercase tracking-wider">
                                        {formatCategory(p.category)}
                                    </span>
                                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                        <button
                                            onClick={() =>
                                                setEditing({
                                                    id: p.id,
                                                    title: p.title,
                                                    description: p.description ?? "",
                                                    category: p.category,
                                                    content: p.content,
                                                    isPublic: p.isPublic,
                                                })
                                            }
                                            className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
                                            title="编辑"
                                        >
                                            <Pencil className="w-3.5 h-3.5" />
                                        </button>
                                        <button
                                            onClick={() => handleDelete(p.id)}
                                            disabled={busyId === p.id}
                                            className="p-1 rounded hover:bg-rose-500/10 text-[#6e6b63] dark:text-[#a19f96] hover:text-rose-600 disabled:opacity-50"
                                            title="删除"
                                        >
                                            {busyId === p.id ? (
                                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                            ) : (
                                                <Trash2 className="w-3.5 h-3.5" />
                                            )}
                                        </button>
                                    </div>
                                </div>
                                <h4 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                                    {p.title}
                                </h4>
                                <p className="text-xs text-[#6e6b63] dark:text-[#a19f96] leading-relaxed line-clamp-3">
                                    {p.description || p.content}
                                </p>
                            </div>
                            <div className="flex items-center gap-2">
                                <button
                                    onClick={() => handleUse(p)}
                                    className="flex-1 py-2.5 bg-[#f3f0e6] hover:bg-[#da7756] text-[#1f1e1d] dark:text-[#edece8] hover:text-white text-xs font-medium rounded-xl transition-all flex items-center justify-center gap-2 border border-[#e3dfd5] dark:border-[#2e2d2a]"
                                >
                                    <Terminal className="w-3.5 h-3.5" />
                                    使用
                                </button>
                                <button
                                    onClick={() => handleCopy(p)}
                                    className="px-3 py-2.5 bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#201f1c] dark:hover:bg-[#262522] text-[#6e6b63] dark:text-[#a19f96] text-xs font-medium rounded-xl transition-all border border-[#e3dfd5] dark:border-[#2e2d2a]"
                                    title="复制内容"
                                >
                                    <Copy className="w-3.5 h-3.5" />
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {/* 编辑/新建 Modal */}
            {editing && (
                <div
                    className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4"
                    onClick={() => !saving && setEditing(null)}
                >
                    <div
                        className="bg-[#fbf9f5] dark:bg-[#1a1917] rounded-2xl border border-[#e6e2d8] dark:border-[#282724] shadow-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between p-5 border-b border-[#e6e2d8] dark:border-[#282724]">
                            <h3 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8] flex items-center gap-2">
                                <Sparkles className="w-4 h-4 text-[#da7756]" />
                                {editing.id ? "编辑提示词" : "新建提示词"}
                            </h3>
                            <button
                                onClick={() => !saving && setEditing(null)}
                                className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[#6e6b63]"
                            >
                                <X className="w-4 h-4" />
                            </button>
                        </div>
                        <div className="p-5 space-y-4">
                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                                        标题 *
                                    </label>
                                    <input
                                        value={editing.title}
                                        onChange={(e) =>
                                            setEditing({ ...editing, title: e.target.value })
                                        }
                                        className="w-full bg-white dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-3.5 py-2.5 text-xs text-[#1f1e1d] dark:text-[#edece8]"
                                        placeholder="如：代码重构专家"
                                    />
                                </div>
                                <div>
                                    <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                                        分类
                                    </label>
                                    <input
                                        value={editing.category}
                                        onChange={(e) =>
                                            setEditing({ ...editing, category: e.target.value })
                                        }
                                        className="w-full bg-white dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-3.5 py-2.5 text-xs text-[#1f1e1d] dark:text-[#edece8]"
                                        placeholder="如：通用、工程开发、写作"
                                    />
                                </div>
                            </div>
                            <div>
                                <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                                    描述
                                </label>
                                <input
                                    value={editing.description}
                                    onChange={(e) =>
                                        setEditing({ ...editing, description: e.target.value })
                                    }
                                    className="w-full bg-white dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-3.5 py-2.5 text-xs text-[#1f1e1d] dark:text-[#edece8]"
                                    placeholder="简短描述该模板的用途"
                                />
                            </div>
                            <div>
                                <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                                    模板内容 *{" "}
                                    <span className="normal-case font-normal text-[10px]">
                                        （可用 {"{input}"} 作为用户输入占位符）
                                    </span>
                                </label>
                                <textarea
                                    value={editing.content}
                                    onChange={(e) =>
                                        setEditing({ ...editing, content: e.target.value })
                                    }
                                    rows={8}
                                    className="w-full bg-white dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-3.5 py-2.5 text-xs text-[#1f1e1d] dark:text-[#edece8] font-mono resize-y"
                                    placeholder="你是一名... 用户输入：{input}"
                                />
                            </div>
                            <label className="flex items-center gap-2 text-xs text-[#1f1e1d] dark:text-[#edece8] cursor-pointer">
                                <input
                                    type="checkbox"
                                    checked={editing.isPublic}
                                    onChange={(e) =>
                                        setEditing({ ...editing, isPublic: e.target.checked })
                                    }
                                    className="rounded"
                                />
                                公开（其他用户可见）
                            </label>
                        </div>
                        <div className="flex justify-end gap-2 p-5 border-t border-[#e6e2d8] dark:border-[#282724]">
                            <button
                                onClick={() => setEditing(null)}
                                disabled={saving}
                                className="px-4 py-2 text-xs font-medium rounded-xl bg-[#f3f0e6] hover:bg-[#eae6db] dark:bg-[#201f1c] dark:hover:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] border border-[#e3dfd5] dark:border-[#2e2d2a] disabled:opacity-50"
                            >
                                取消
                            </button>
                            <button
                                onClick={handleSave}
                                disabled={saving}
                                className="px-4 py-2 text-xs font-medium rounded-xl bg-[#da7756] hover:bg-[#c86544] text-white disabled:opacity-50 flex items-center gap-2"
                            >
                                {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                                保存
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};
