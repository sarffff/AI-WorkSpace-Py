import React, { useEffect } from "react";
import { useSelector, useDispatch } from "react-redux";
import { RootState } from "@/app/providers/store";
import {
    setSelectedModel,
    setServerStatus,
} from "@/entities/chat/model/chatSlice";
import { apiClient } from "@/shared/api/client";
import { ChevronDown, ShieldCheck } from "lucide-react";

export const Header: React.FC = () => {
    const dispatch = useDispatch();
    const { selectedModel, activeTab, serverStatus, sessions } = useSelector(
        (state: RootState) => state.chat,
    );
    const [appVersion] = React.useState<string>("0.1.0");

    useEffect(() => {
        apiClient.ping().then((online) => {
            dispatch(setServerStatus(online ? "online" : "offline"));
        });
    }, [dispatch]);

    const models = [
        "glm-4.5-air",
        "gpt-6",
        "Claude-Opus-5",
        "DeepSeek-V4",
        "Gemini-3.5-Pro",
    ];

    const titles: Record<string, string> = {
        chat: "AI Workspace Assistant",
        knowledge: "Knowledge Base & RAG",
        prompts: "Prompt Engineering Hub",
        settings: "Application Settings",
    };

    const statusColor =
        serverStatus === "online"
            ? "text-emerald-400"
            : serverStatus === "offline"
              ? "text-red-400"
              : "text-yellow-400";
    const statusLabel =
        serverStatus === "online"
            ? `Online (${sessions.length} chats)`
            : serverStatus === "offline"
              ? "Offline"
              : "Checking...";

    return (
        <header className="h-14 border-b border-slate-800/80 bg-slate-900/40 backdrop-blur px-6 flex items-center justify-between select-none">
            <div className="flex items-center gap-3">
                <h2 className="text-sm font-semibold text-slate-100">
                    {titles[activeTab] || "Dashboard"}
                </h2>
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 font-medium">
                    v{appVersion}
                </span>
            </div>

            <div className="flex items-center gap-4">
                <div className="relative group">
                    <select
                        value={selectedModel}
                        onChange={(e) =>
                            dispatch(setSelectedModel(e.target.value))
                        }
                        className="appearance-none bg-slate-800/80 hover:bg-slate-800 text-slate-200 text-xs rounded-lg px-3 py-1.5 pr-8 border border-slate-700/60 focus:outline-none focus:ring-1 focus:ring-indigo-500 cursor-pointer font-medium transition-all"
                    >
                        {models.map((m) => (
                            <option
                                key={m}
                                value={m}
                                className="bg-slate-900 text-slate-200"
                            >
                                {m}
                            </option>
                        ))}
                    </select>
                    <ChevronDown className="w-3.5 h-3.5 text-slate-400 absolute right-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
                </div>

                <div className="flex items-center gap-2 px-2.5 py-1 rounded-lg bg-slate-950/40 border border-slate-800/60 text-[11px] text-slate-300">
                    <ShieldCheck className={`w-3.5 h-3.5 ${statusColor}`} />
                    <span>
                        Server:{" "}
                        <strong className={`${statusColor} font-medium`}>
                            {statusLabel}
                        </strong>
                    </span>
                </div>
            </div>
        </header>
    );
};
