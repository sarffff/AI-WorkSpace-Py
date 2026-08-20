import React, { useEffect, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useNavigate } from "react-router-dom";
import { RootState } from "@/app/providers/store";
import { apiClient } from "@/shared/api/client";
import { setPromptVersion } from "@/entities/chat/model/chatSlice";
import type { PromptLibraryEntry } from "@/shared/types/api.types";
import {
    FlaskConical,
    Loader2,
    Lock,
    RotateCcw,
} from "lucide-react";
import { EntryCard, type ViewMode } from "../components/EntryCard";

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

