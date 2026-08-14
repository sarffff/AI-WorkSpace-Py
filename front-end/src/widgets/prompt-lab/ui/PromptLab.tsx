import React, { useEffect, useMemo, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useNavigate } from "react-router-dom";
import { RootState } from "@/app/providers/store";
import { apiClient } from "@/shared/api/client";
import { setPromptVersion } from "@/entities/chat/model/chatSlice";
import { diffLines, countDiff } from "@/shared/lib/diffLines";
import type {
    PromptLibraryEntry,
    PromptStatus,
} from "@/shared/types/api.types";
import {
    FlaskConical,
    Loader2,
    Lock,
    GitCompare,
    FileText,
    RotateCcw,
    Play,
    AlertTriangle,
} from "lucide-react";

const STATUS_STYLE: Record<PromptStatus, { label: string; className: string }> = {
    active: {
        label: "生效中",
        className: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
    },
    candidate: {
        label: "候选",
        className: "bg-[#da7756]/10 text-[#da7756]",
    },
    archived: {
        label: "已归档",
        className: "bg-[#918d83]/10 text-[#918d83]",
    },
};

type ViewMode = "body" | "diff";

/**
 * 提示词实验台：看清每一版系统提示词的差异，并把某一版挂到下一轮对话上。
 *
 * 这里刻意不提供编辑框。版本是仓库里的文件（back-end/prompts/），
 * 能在线改就意味着"上次那组评估数字是哪版提示词跑的"没人答得上来。
 */
export const PromptLab: React.FC = () => {
    const dispatch = useDispatch();
    const navigate = useNavigate();
    const { promptVersion } = useSelector((state: RootState) => state.chat);

    const [entries, setEntries] = useState<PromptLibraryEntry[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [selected, setSelected] = useState<Record<string, string>>({});
    const [mode, setMode] = useState<Record<string, ViewMode>>({});

    useEffect(() => {
        setLoading(true);
        apiClient
            .getPromptLibrary()
            .then((data) => {
                setEntries(data);
                // 默认选中生效版本——先看清当前在跑什么，再去比别的版本
                setSelected(
                    Object.fromEntries(data.map((e) => [e.key, e.activeVersion])),
                );
            })
            .catch((e) =>
                setError(e instanceof Error ? e.message : "提示词注册表加载失败"),
            )
            .finally(() => setLoading(false));
    }, []);

    if (loading) {
        return (
            <div className="flex justify-center py-10 text-[#918d83]">
                <Loader2 className="w-5 h-5 animate-spin" />
            </div>
        );
    }

    if (error) {
        return (
            <div className="card-surface p-4 rounded-2xl text-xs text-rose-600 dark:text-rose-400">
                {error}
            </div>
        );
    }

    return (
        <div className="space-y-4 relative z-10">
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h4 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8] flex items-center gap-2">
                        <FlaskConical className="w-4 h-4 text-[#da7756]" />
                        系统提示词实验台
                    </h4>
                    <p className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] mt-1 leading-relaxed">
                        驱动对话与评估的系统提示词，每一版都是{" "}
                        <code className="font-mono text-[10px]">
                            back-end/prompts/
                        </code>{" "}
                        下的一个文件。这里只读：能在线改，就没法回答「上次那组评估数字是哪版跑的」。
                    </p>
                </div>
                <span className="shrink-0 flex items-center gap-1.5 text-[10px] text-[#918d83] px-2.5 py-1 rounded-full bg-[#918d83]/10">
                    <Lock className="w-3 h-3" />
                    只读
                </span>
            </div>

            {promptVersion && (
                <div className="flex items-center justify-between gap-3 p-3 rounded-xl bg-[#da7756]/10 border border-[#da7756]/25 text-[11px]">
                    <span className="text-[#1f1e1d] dark:text-[#edece8]">
                        下一轮对话将使用{" "}
                        <span className="font-mono font-semibold">{promptVersion}</span>
                        ，只影响你自己的请求，不改服务端默认版本。
                    </span>
                    <button
                        onClick={() => dispatch(setPromptVersion(null))}
                        className="shrink-0 flex items-center gap-1 px-2.5 py-1 rounded-lg bg-white/60 dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
                    >
                        <RotateCcw className="w-3 h-3" />
                        回到默认版
                    </button>
                </div>
            )}

            {entries.map((entry) => (
                <EntryCard
                    key={entry.key}
                    entry={entry}
                    selectedVersion={selected[entry.key] ?? entry.activeVersion}
                    mode={mode[entry.key] ?? "body"}
                    pinnedVersion={promptVersion}
                    onSelect={(version) =>
                        setSelected((prev) => ({ ...prev, [entry.key]: version }))
                    }
                    onMode={(next) =>
                        setMode((prev) => ({ ...prev, [entry.key]: next }))
                    }
                    onTry={(version) => {
                        dispatch(setPromptVersion(version));
                        navigate("/chat");
                    }}
                />
            ))}
        </div>
    );
};

interface EntryCardProps {
    entry: PromptLibraryEntry;
    selectedVersion: string;
    mode: ViewMode;
    pinnedVersion: string | null;
    onSelect: (version: string) => void;
    onMode: (mode: ViewMode) => void;
    onTry: (version: string) => void;
}

const EntryCard: React.FC<EntryCardProps> = ({
    entry,
    selectedVersion,
    mode,
    pinnedVersion,
    onSelect,
    onMode,
    onTry,
}) => {
    const current =
        entry.versions.find((v) => v.version === selectedVersion) ??
        entry.versions[0];
    const active = entry.versions.find((v) => v.isActive);

    const diff = useMemo(() => {
        if (!active || !current || active.version === current.version) return null;
        return diffLines(active.body, current.body);
    }, [active, current]);

    const counts = diff ? countDiff(diff) : null;
    const delta = active && current ? current.chars - active.chars : 0;

    return (
        <div className="card-surface p-5 rounded-2xl space-y-4">
            <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                    <div className="flex items-center gap-2">
                        <code className="font-mono text-xs font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                            {entry.key}
                        </code>
                        {!entry.switchable && (
                            <span className="text-[10px] px-2 py-0.5 rounded-full bg-[#918d83]/10 text-[#918d83]">
                                无版本开关
                            </span>
                        )}
                    </div>
                    <p className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] mt-1">
                        {entry.purpose}
                    </p>
                    {(entry.placeholders.length > 0 || entry.flags.length > 0) && (
                        <p className="text-[10px] text-[#918d83] mt-1 font-mono">
                            {entry.placeholders.map((p) => `{${p}}`).join(" ")}
                            {entry.flags.length > 0 &&
                                ` [[if ${entry.flags.join(" / ")}]]`}
                        </p>
                    )}
                </div>
                {entry.setting && (
                    <span className="shrink-0 text-[10px] font-mono text-[#918d83]">
                        {entry.setting}
                    </span>
                )}
            </div>

            {/* 版本切换 */}
            <div className="flex flex-wrap items-center gap-2">
                {entry.versions.map((version) => (
                    <button
                        key={version.version}
                        onClick={() => onSelect(version.version)}
                        className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-xl text-[11px] font-medium border transition-colors ${
                            version.version === selectedVersion
                                ? "bg-[#da7756] text-white border-[#da7756]"
                                : "bg-[#f3f0e6] dark:bg-[#201f1c] text-[#1f1e1d] dark:text-[#edece8] border-[#e3dfd5] dark:border-[#2e2d2a] hover:border-[#da7756]/50"
                        }`}
                    >
                        <span className="font-mono">{version.version}</span>
                        <span
                            className={`text-[9px] px-1.5 py-0.5 rounded-full ${
                                version.version === selectedVersion
                                    ? "bg-white/20 text-white"
                                    : STATUS_STYLE[version.status].className
                            }`}
                        >
                            {STATUS_STYLE[version.status].label}
                        </span>
                    </button>
                ))}
            </div>

            {current && (
                <div className="space-y-3">
                    <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0">
                            <div className="text-xs font-medium text-[#1f1e1d] dark:text-[#edece8] truncate">
                                {current.label}
                            </div>
                            <div className="text-[10px] text-[#918d83] mt-0.5">
                                {current.chars} 字
                                {delta !== 0 && active && (
                                    <>
                                        {" · 相比生效版 "}
                                        <span
                                            className={
                                                delta < 0
                                                    ? "text-emerald-600 dark:text-emerald-400"
                                                    : "text-amber-600 dark:text-amber-400"
                                            }
                                        >
                                            {delta > 0 ? "+" : ""}
                                            {delta}
                                        </span>
                                        {counts &&
                                            ` · +${counts.added} / -${counts.removed} 行`}
                                    </>
                                )}
                            </div>
                        </div>
                        {diff && (
                            <div className="shrink-0 flex items-center gap-1 p-0.5 rounded-lg bg-[#f3f0e6] dark:bg-[#201f1c]">
                                <ModeButton
                                    active={mode === "body"}
                                    onClick={() => onMode("body")}
                                    icon={<FileText className="w-3 h-3" />}
                                    label="正文"
                                />
                                <ModeButton
                                    active={mode === "diff"}
                                    onClick={() => onMode("diff")}
                                    icon={<GitCompare className="w-3 h-3" />}
                                    label="对比生效版"
                                />
                            </div>
                        )}
                    </div>

                    {current.notes && (
                        <p className="text-[11px] text-[#6e6b63] dark:text-[#a19f96] leading-relaxed p-3 rounded-xl bg-[#faf9f5] dark:bg-[#191817]">
                            {current.notes}
                        </p>
                    )}

                    {current.status === "archived" && (
                        <p className="flex items-start gap-1.5 text-[11px] text-amber-600 dark:text-amber-400">
                            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                            这一版只作对照组用。可以试，但别当成能上线的选项。
                        </p>
                    )}

                    <div className="rounded-xl border border-[#e6e2d8] dark:border-[#282724] overflow-hidden">
                        {mode === "diff" && diff ? (
                            <div className="font-mono text-[10.5px] leading-relaxed">
                                {diff.map((line, index) => (
                                    <div
                                        key={index}
                                        className={`px-3 py-1 whitespace-pre-wrap break-words ${
                                            line.op === "added"
                                                ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                                                : line.op === "removed"
                                                  ? "bg-rose-500/10 text-rose-700 dark:text-rose-300"
                                                  : "text-[#6e6b63] dark:text-[#a19f96]"
                                        }`}
                                    >
                                        <span className="select-none opacity-60 mr-2">
                                            {line.op === "added"
                                                ? "+"
                                                : line.op === "removed"
                                                  ? "-"
                                                  : " "}
                                        </span>
                                        {line.text || " "}
                                    </div>
                                ))}
                            </div>
                        ) : (
                            <pre className="px-3 py-2.5 font-mono text-[10.5px] leading-relaxed whitespace-pre-wrap break-words text-[#1f1e1d] dark:text-[#edece8] max-h-72 overflow-y-auto">
                                {current.body}
                            </pre>
                        )}
                    </div>

                    {entry.requestOverridable ? (
                        <div className="flex items-center gap-2">
                            <button
                                onClick={() => onTry(current.version)}
                                disabled={pinnedVersion === current.version}
                                className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-[11px] font-medium bg-[#f3f0e6] hover:bg-[#da7756] dark:bg-[#201f1c] text-[#1f1e1d] dark:text-[#edece8] hover:text-white border border-[#e3dfd5] dark:border-[#2e2d2a] hover:border-[#da7756] transition-colors disabled:opacity-50 disabled:hover:bg-[#f3f0e6] disabled:hover:text-[#1f1e1d]"
                            >
                                <Play className="w-3 h-3" />
                                {pinnedVersion === current.version
                                    ? "已挂在下一轮对话上"
                                    : "用这版试一轮"}
                            </button>
                            <span className="text-[10px] text-[#918d83]">
                                只作用于你自己的请求；语义缓存按版本分桶，不会命中别版的旧答案。
                            </span>
                        </div>
                    ) : (
                        <p className="text-[10px] text-[#918d83]">
                            这类提示词不支持按请求覆盖：它服务于离线评估，一轮跑下来的配置
                            必须完全由代码决定，否则报告里的数字没法复现。改它请用{" "}
                            <code className="font-mono">{entry.setting ?? "配置项"}</code>
                            。
                        </p>
                    )}
                </div>
            )}
        </div>
    );
};

const ModeButton: React.FC<{
    active: boolean;
    onClick: () => void;
    icon: React.ReactNode;
    label: string;
}> = ({ active, onClick, icon, label }) => (
    <button
        onClick={onClick}
        className={`flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-medium transition-colors ${
            active
                ? "bg-white dark:bg-[#262522] text-[#1f1e1d] dark:text-[#edece8] shadow-sm"
                : "text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
        }`}
    >
        {icon}
        {label}
    </button>
);
