import React, { useEffect, useState } from "react";
import { useDispatch } from "react-redux";
import { apiClient } from "@/shared/api/client";
import type { AppSettings, UserPreferences } from "@/shared/types/api.types";
import { PageHeader } from "@/shared/ui/PageHeader";
import { setSelectedModel } from "@/entities/chat/model/chatSlice";
import { useNavigate } from "react-router-dom";
import { Server, Cpu, X, Loader2, Save, Check, Database, ArrowRight, Wrench, Paperclip, Globe, BookPlus, History, Users, ShieldCheck } from "lucide-react";
import { CapCell } from "../components/CapCell";
import { ModeCell } from "../components/ModeCell";
import { ConfigRow } from "../components/ConfigRow";
import { StatusCard } from "../components/StatusCard";
import { MemoryPanel } from "../components/MemoryPanel";

export const SettingsPage: React.FC = () => {
    const dispatch = useDispatch();
    const navigate = useNavigate();
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
        <div className="page-shell app-atmosphere transition-colors duration-200">
            <div className="relative z-10 space-y-6 max-w-5xl">
            <PageHeader
                eyebrow="系统"
                title="设置"
                description="模型参数、长期记忆、服务端配置，以及后端实际注册了哪些能力。"
            />

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

            {/* 后端能力矩阵：不是开关值，是「能不能用」 */}
            {settings?.capabilities && (
                <div className="card-surface p-6 rounded-2xl space-y-4 relative z-10 anim-fade-up">
                    <div className="flex items-center gap-2.5 pb-3 border-b border-[#e6e2d8] dark:border-[#282724]">
                        <span className="w-6 h-6 rounded-lg bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
                            <Wrench className="w-3.5 h-3.5" />
                        </span>
                        <div>
                            <h4 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                                已注册能力
                            </h4>
                            <p className="text-[11px] text-[#918d83] mt-0.5">
                                开关打开但没配密钥时工具根本不注册，这里报的是「能不能用」。
                            </p>
                        </div>
                    </div>
                    <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                        <CapCell
                            icon={<Cpu className="w-3.5 h-3.5" />}
                            label="计算器"
                            on={settings.capabilities.calculate}
                        />
                        <CapCell
                            icon={<Paperclip className="w-3.5 h-3.5" />}
                            label="按需读附件"
                            on={settings.capabilities.readAttachment}
                        />
                        <CapCell
                            icon={<Globe className="w-3.5 h-3.5" />}
                            label="联网搜索"
                            on={settings.capabilities.webSearch}
                        />
                        <CapCell
                            icon={<BookPlus className="w-3.5 h-3.5" />}
                            label="写入知识库"
                            on={settings.capabilities.writeKnowledge}
                        />
                        <CapCell
                            icon={<History className="w-3.5 h-3.5" />}
                            label="工具轨迹落库"
                            on={settings.capabilities.toolHistory}
                        />
                        {settings.capabilities.delegation && (
                            <ModeCell
                                icon={<Users className="w-3.5 h-3.5" />}
                                label="多代理委派"
                                active={settings.capabilities.delegation.mode !== "off"}
                                mode={
                                    settings.capabilities.delegation.mode === "augment"
                                        ? "增强模式"
                                        : settings.capabilities.delegation.mode === "supervisor"
                                          ? "主管模式"
                                          : "关闭"
                                }
                                title={`最多委派 ${settings.capabilities.delegation.maxDelegations} 次 · 角色：${settings.capabilities.delegation.roles.join("、") || "无"}`}
                            />
                        )}
                        {settings.capabilities.approval && (
                            <ModeCell
                                icon={<ShieldCheck className="w-3.5 h-3.5" />}
                                label="人工审批"
                                active={settings.capabilities.approval.mode !== "off"}
                                mode={
                                    settings.capabilities.approval.mode === "write"
                                        ? "写操作需确认"
                                        : settings.capabilities.approval.mode === "listed"
                                          ? `白名单 ${settings.capabilities.approval.tools.length} 项`
                                          : "关闭"
                                }
                                title={
                                    settings.capabilities.approval.checkpoints
                                        ? undefined
                                        : "快照未开启，审批不会生效"
                                }
                            />
                        )}
                    </div>
                </div>
            )}

            {/* 模型与生成参数 */}
            {prefs && settings && (
                <div className="card-surface p-6 rounded-2xl space-y-5 relative z-10 anim-fade-up stagger-1">
                    <div className="flex items-center gap-2.5 pb-3 border-b border-[#e6e2d8] dark:border-[#282724]">
                        <span className="w-6 h-6 rounded-lg bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
                            <Cpu className="w-3.5 h-3.5" />
                        </span>
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
                                className="w-full slider-accent"
                                style={
                                    {
                                        "--fill": `${(prefs.temperature / 2) * 100}%`,
                                    } as React.CSSProperties
                                }
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
                                className="w-full slider-accent"
                                style={
                                    {
                                        "--fill": `${prefs.topP * 100}%`,
                                    } as React.CSSProperties
                                }
                            />
                        </div>
                    </div>

                    <div className="flex justify-end pt-2">
                        <button
                            onClick={handleSave}
                            disabled={saving}
                            className="btn-accent px-4 py-2.5 text-white text-xs font-medium rounded-xl flex items-center gap-2 disabled:opacity-50"
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

            {/* 长期记忆：跨会话注入的用户事实与偏好，可查看、可删除 */}
            <MemoryPanel className="anim-fade-up stagger-2" />

            {/* 服务端配置（只读） */}
            {settings && (
                <div className="card-surface p-6 rounded-2xl space-y-4 relative z-10 anim-fade-up stagger-3">
                    <div className="flex items-center gap-2.5 pb-3 border-b border-[#e6e2d8] dark:border-[#282724]">
                        <span className="w-6 h-6 rounded-lg bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
                            <Server className="w-3.5 h-3.5" />
                        </span>
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

            {/* 迁移提示 */}
            <div className="card-surface rounded-2xl p-5 relative z-10 anim-fade-up stagger-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className="w-10 h-10 rounded-xl bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
                    <Server className="w-5 h-5" />
                  </span>
                  <div>
                    <div className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                      用量与运行轨迹
                    </div>
                    <div className="text-xs text-[#6e6b63] dark:text-[#a19f96] mt-0.5">
                      用量统计与 Agent 运行轨迹已移至工作台和运行轨迹页
                    </div>
                  </div>
                </div>
                <button
                  onClick={() => navigate("/dashboard")}
                  className="btn-accent px-4 py-2 text-xs font-medium rounded-xl flex items-center gap-2"
                >
                  <ArrowRight className="w-3.5 h-3.5" />
                  前往
                </button>
              </div>
            </div>

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
        </div>
    );
};
