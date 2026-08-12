import React, { useEffect, useState } from "react";
import { useDispatch } from "react-redux";
import { apiClient } from "@/shared/api/client";
import type { AppSettings, UserPreferences } from "@/shared/types/api.types";
import { setSelectedModel } from "@/entities/chat/model/chatSlice";
import { UsagePanel } from "@/widgets/usage-panel/ui/UsagePanel";
import {
    Loader2,
    Save,
    Check,
    Server,
    Cpu,
    Database,
    X,
} from "lucide-react";

export const SettingsPage: React.FC = () => {
    const dispatch = useDispatch();
    const [settings, setSettings] = useState<AppSettings | null>(null);
    const [prefs, setPrefs] = useState<UserPreferences | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [saved, setSaved] = useState(false);

    useEffect(() => {
        apiClient
            .getSettings()
            .then((s) => {
                setSettings(s);
                setPrefs(s.preferences);
                dispatch(setSelectedModel(s.preferences.defaultModel));
            })
            .catch((e) =>
                setError(e instanceof Error ? e.message : "加载设置失败"),
            )
            .finally(() => setLoading(false));
    }, [dispatch]);

    const handleSave = async () => {
        if (!prefs) return;
        setSaving(true);
        setError(null);
        setSaved(false);
        try {
            const res = await apiClient.updatePreferences({
                defaultModel: prefs.defaultModel,
                temperature: prefs.temperature,
                maxTokens: prefs.maxTokens,
                topP: prefs.topP,
            });
            setPrefs(res.preferences);
            dispatch(setSelectedModel(res.preferences.defaultModel));
            setSaved(true);
            setTimeout(() => setSaved(false), 2000);
        } catch (e) {
            setError(e instanceof Error ? e.message : "保存失败");
        } finally {
            setSaving(false);
        }
    };

    if (loading) {
        return (
            <div className="flex justify-center items-center h-full text-[#918d83]">
                <Loader2 className="w-6 h-6 animate-spin" />
            </div>
        );
    }

    return (
        <div className="p-8 h-full overflow-y-auto space-y-6 bg-[#fbf9f5] dark:bg-[#141413] transition-colors duration-200">
            <div>
                <h3 className="text-lg font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                    设置
                </h3>
                <p className="text-xs text-[#6e6b63] dark:text-[#a19f96] mt-1">
                    管理模型、生成参数与服务端配置。
                </p>
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

            {/* 模型与生成参数 */}
            {prefs && settings && (
                <div className="p-6 rounded-2xl bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] space-y-5 shadow-sm">
                    <div className="flex items-center gap-2 pb-3 border-b border-[#e6e2d8] dark:border-[#282724]">
                        <Cpu className="w-4 h-4 text-[#da7756]" />
                        <h4 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                            模型与生成参数
                        </h4>
                    </div>

                    <div className="grid grid-cols-2 gap-5">
                        <div>
                            <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                                默认模型
                            </label>
                            <select
                                value={prefs.defaultModel}
                                onChange={(e) =>
                                    setPrefs({ ...prefs, defaultModel: e.target.value })
                                }
                                className="w-full bg-white dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-3.5 py-2.5 text-xs text-[#1f1e1d] dark:text-[#edece8]"
                            >
                                {settings.availableModels.map((m) => (
                                    <option key={m.id} value={m.id}>
                                        {m.label} ({m.provider})
                                    </option>
                                ))}
                            </select>
                        </div>
                        <div>
                            <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                                最大输出令牌数
                            </label>
                            <input
                                type="number"
                                min={128}
                                max={8192}
                                value={prefs.maxTokens}
                                onChange={(e) =>
                                    setPrefs({
                                        ...prefs,
                                        maxTokens: Number(e.target.value) || 0,
                                    })
                                }
                                className="w-full bg-white dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-3.5 py-2.5 text-xs text-[#1f1e1d] dark:text-[#edece8]"
                            />
                        </div>
                        <div>
                            <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                                生成温度：{prefs.temperature.toFixed(2)}
                            </label>
                            <input
                                type="range"
                                min={0}
                                max={2}
                                step={0.05}
                                value={prefs.temperature}
                                onChange={(e) =>
                                    setPrefs({
                                        ...prefs,
                                        temperature: Number(e.target.value),
                                    })
                                }
                                className="w-full accent-[#da7756]"
                            />
                        </div>
                        <div>
                            <label className="block text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96] mb-1.5">
                                核采样 Top P：{prefs.topP.toFixed(2)}
                            </label>
                            <input
                                type="range"
                                min={0}
                                max={1}
                                step={0.05}
                                value={prefs.topP}
                                onChange={(e) =>
                                    setPrefs({ ...prefs, topP: Number(e.target.value) })
                                }
                                className="w-full accent-[#da7756]"
                            />
                        </div>
                    </div>

                    <div className="flex justify-end pt-2">
                        <button
                            onClick={handleSave}
                            disabled={saving}
                            className="px-4 py-2.5 bg-[#da7756] hover:bg-[#c86544] text-white text-xs font-medium rounded-xl flex items-center gap-2 disabled:opacity-50 shadow-md shadow-[#da7756]/20 transition-all"
                        >
                            {saving ? (
                                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : saved ? (
                                <Check className="w-3.5 h-3.5" />
                            ) : (
                                <Save className="w-3.5 h-3.5" />
                            )}
                            {saving ? "保存中..." : saved ? "已保存" : "保存"}
                        </button>
                    </div>
                </div>
            )}

            {/* 服务端配置（只读） */}
            {settings && (
                <div className="p-6 rounded-2xl bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] space-y-4 shadow-sm">
                    <div className="flex items-center gap-2 pb-3 border-b border-[#e6e2d8] dark:border-[#282724]">
                        <Server className="w-4 h-4 text-[#da7756]" />
                        <h4 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                            服务端配置
                        </h4>
                    </div>
                    <div className="grid grid-cols-2 gap-5 text-xs">
                        <ConfigRow label="大模型服务地址" value={settings.server.llmBaseUrl} />
                        <ConfigRow
                            label="已配置模型"
                            value={settings.server.configuredModel}
                        />
                        <ConfigRow
                            label="向量模型"
                            value={settings.server.embeddingModel}
                        />
                        <ConfigRow
                            label="Redis"
                            value={settings.server.redisEnabled ? "已启用" : "未启用"}
                            badge={settings.server.redisEnabled ? "ok" : undefined}
                        />
                        <div className="col-span-2">
                            <ConfigRow
                                label="数据库地址"
                                value={settings.server.databaseUrl}
                            />
                        </div>
                    </div>
                </div>
            )}

            {/* 用量与成本 */}
            <UsagePanel />

            {/* 数据库状态卡 */}
            <div className="grid grid-cols-3 gap-4">
                <StatusCard
                    icon={<Database className="w-5 h-5" />}
                    title="数据库"
                    value="MySQL"
                    ok
                />
                <StatusCard
                    icon={<Server className="w-5 h-5" />}
                    title="Redis"
                    value={settings?.server.redisEnabled ? "已连接" : "未启用"}
                    ok={settings?.server.redisEnabled}
                />
                <StatusCard
                    icon={<Cpu className="w-5 h-5" />}
                    title="大模型服务商"
                    value="智谱 / OpenAI"
                    ok
                />
            </div>
        </div>
    );
};

const ConfigRow: React.FC<{ label: string; value: string; badge?: "ok" }> = ({
    label,
    value,
    badge,
}) => (
    <div>
        <div className="text-[10px] font-semibold tracking-wide text-[#6e6b63] dark:text-[#a19f96] mb-1">
            {label}
        </div>
        <div className="flex items-center gap-2">
            <code className="text-xs text-[#1f1e1d] dark:text-[#edece8] font-mono break-all">
                {value}
            </code>
            {badge === "ok" && (
                <span className="px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 text-[10px] font-semibold">
                    已启用
                </span>
            )}
        </div>
    </div>
);

const StatusCard: React.FC<{
    icon: React.ReactNode;
    title: string;
    value: string;
    ok?: boolean;
}> = ({ icon, title, value, ok }) => (
    <div className="p-5 rounded-2xl bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] space-y-1 shadow-sm">
        <div
            className={`flex items-center gap-2 text-xs font-semibold tracking-wide ${
                ok
                    ? "text-emerald-600 dark:text-emerald-400"
                    : "text-[#6e6b63] dark:text-[#a19f96]"
            }`}
        >
            {icon}
            {title}
        </div>
        <div className="text-lg font-bold text-[#1f1e1d] dark:text-[#edece8]">
            {value}
        </div>
    </div>
);
