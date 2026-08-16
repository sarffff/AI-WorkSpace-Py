import React, { useEffect, useRef, useState } from "react";
import { RetrievalDebugger } from "@/features/knowledge-debug/ui/RetrievalDebugger";
import { apiClient } from "@/shared/api/client";
import type { KnowledgeDocument } from "@/shared/types/api.types";
import { PageHeader } from "@/shared/ui/PageHeader";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import {
    Upload,
    FileText,
    Search,
    ShieldCheck,
    Trash2,
    Loader2,
    X,
    FlaskConical,
    Layers,
} from "lucide-react";

function formatSize(bytes: number): string {
    if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
    if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(0)} KB`;
    return `${bytes} 字节`;
}

const DOCUMENT_STATUS_LABELS: Record<KnowledgeDocument["status"], string> = {
    indexed: "已完成",
    processing: "处理中",
    failed: "处理失败",
};

export const KnowledgePage: React.FC = () => {
    const [activeTab, setActiveTab] = useState<"documents" | "debug">("documents");
    const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
    const [totalDocs, setTotalDocs] = useState(0);
    const [totalChunks, setTotalChunks] = useState(0);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(false);
    const [searchQuery, setSearchQuery] = useState("");
    const [deletingId, setDeletingId] = useState<string | null>(null);
    const [errorMsg, setErrorMsg] = useState<string | null>(null);
    const [dragOver, setDragOver] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const toast = useToast();

    const refreshDocuments = (showSpinner = true) => {
        if (showSpinner) setLoading(true);
        apiClient
            .getDocuments()
            .then((docs) => {
                setDocuments(docs);
                setTotalDocs(docs.length);
                setTotalChunks(docs.reduce((sum, d) => sum + d.chunks, 0));
            })
            .catch((e) => {
                // 轮询（showSpinner=false，每 2s 一次）失败保持安静，只有
                // 首次加载和手动刷新才提示，否则处理中的文档会刷屏
                if (showSpinner) {
                    toast.error(toastMessageFrom(e, "加载知识库文档失败"));
                }
            })
            .finally(() => {
                if (showSpinner) setLoading(false);
            });
    };

    useEffect(() => {
        refreshDocuments();
    }, []);

    const hasProcessing = documents.some((doc) => doc.status === "processing");
    useEffect(() => {
        if (!hasProcessing) return;
        const timer = setInterval(() => refreshDocuments(false), 2000);
        return () => clearInterval(timer);
    }, [hasProcessing]);

    const handleUploadClick = () => {
        fileInputRef.current?.click();
    };

    const uploadFile = async (file: File) => {
        setUploading(true);
        setErrorMsg(null);
        try {
            await apiClient.uploadDocument(file);
            refreshDocuments();
        } catch (err) {
            setErrorMsg(err instanceof Error ? err.message : "上传失败");
        } finally {
            setUploading(false);
        }
    };

    const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        e.target.value = "";
        await uploadFile(file);
    };

    const handleDrop = async (e: React.DragEvent) => {
        e.preventDefault();
        setDragOver(false);
        const file = e.dataTransfer.files?.[0];
        if (file) await uploadFile(file);
    };

    const handleDelete = async (docId: string) => {
        if (!window.confirm("确定删除该文档及其向量分块？")) return;
        setDeletingId(docId);
        setErrorMsg(null);
        try {
            await apiClient.deleteDocument(docId);
            refreshDocuments();
        } catch (err) {
            setErrorMsg(err instanceof Error ? err.message : "删除失败");
        } finally {
            setDeletingId(null);
        }
    };

    const filteredDocs = searchQuery.trim()
        ? documents.filter((d) =>
              d.name.toLowerCase().includes(searchQuery.trim().toLowerCase()),
          )
        : documents;

    return (
        <div className="page-shell app-atmosphere transition-colors duration-200">
            <div className="relative z-10 space-y-6 max-w-6xl">
                <PageHeader
                    eyebrow="Retrieval"
                    title="知识库与 RAG"
                    description="文档进库、切块、混合检索。调试页不经过对话，直接看 dense / sparse 命中了什么。"
                    actions={
                        <button
                            onClick={handleUploadClick}
                            disabled={uploading}
                            className="btn-accent px-4 py-2.5 text-white text-xs font-medium rounded-xl flex items-center gap-2 disabled:opacity-60"
                        >
                            {uploading ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                            ) : (
                                <Upload className="w-4 h-4" />
                            )}
                            {uploading ? "上传中..." : "上传文档"}
                        </button>
                    }
                />
                <input
                    ref={fileInputRef}
                    type="file"
                    className="hidden"
                    accept=".txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.xml,.yaml,.yml,.css,.html,.pdf"
                    onChange={handleFileChange}
                />

                <div className="seg-switch w-fit">
                    <button
                        data-active={activeTab === "documents"}
                        onClick={() => setActiveTab("documents")}
                    >
                        <FileText className="w-3.5 h-3.5 inline mr-1.5 -mt-0.5" />
                        文档管理
                    </button>
                    <button
                        data-active={activeTab === "debug"}
                        onClick={() => setActiveTab("debug")}
                    >
                        <FlaskConical className="w-3.5 h-3.5 inline mr-1.5 -mt-0.5" />
                        检索调试
                    </button>
                </div>

                {activeTab === "documents" ? (
                    <div className="space-y-6">
                        {errorMsg && (
                            <div className="flex items-center justify-between gap-3 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs">
                                <span>{errorMsg}</span>
                                <button
                                    onClick={() => setErrorMsg(null)}
                                    className="p-0.5 hover:bg-rose-500/10 rounded"
                                >
                                    <X className="w-3.5 h-3.5" />
                                </button>
                            </div>
                        )}

                        <div
                            className="drop-zone p-6 text-center anim-fade-up"
                            data-active={dragOver}
                            onDragOver={(e) => {
                                e.preventDefault();
                                setDragOver(true);
                            }}
                            onDragLeave={() => setDragOver(false)}
                            onDrop={handleDrop}
                            onClick={handleUploadClick}
                        >
                            <Upload className="w-5 h-5 text-[#da7756] mx-auto mb-2" />
                            <div className="text-xs font-medium text-[#1f1e1d] dark:text-[#edece8]">
                                拖入文档，或点击选择
                            </div>
                            <div className="text-[11px] text-[#918d83] mt-1">
                                txt / md / pdf / 代码文件 · 上传后后台切块并向量化
                            </div>
                        </div>

                        <div className="grid grid-cols-3 gap-4 anim-fade-up stagger-1">
                            <StatCard
                                label="文档总数"
                                value={loading ? "..." : String(totalDocs)}
                                icon={<FileText className="w-4 h-4" />}
                            />
                            <StatCard
                                label="向量分块"
                                value={loading ? "..." : String(totalChunks)}
                                accent
                                icon={<Layers className="w-4 h-4" />}
                            />
                            <StatCard
                                label="索引状态"
                                value={hasProcessing ? "处理中" : "就绪"}
                                ok={!hasProcessing}
                                icon={<ShieldCheck className="w-4 h-4" />}
                            />
                        </div>

                        <div className="card-surface rounded-2xl overflow-hidden anim-fade-up stagger-2">
                            <div className="p-4 border-b border-[#e6e2d8] dark:border-[#282724] flex items-center justify-between">
                                <div className="flex items-center gap-2 bg-[#f3f0e6] dark:bg-[#201f1c] px-3.5 py-2 rounded-xl border border-[#e3dfd5] dark:border-[#2e2d2a] w-72">
                                    <Search className="w-4 h-4 text-[#918d83]" />
                                    <input
                                        type="text"
                                        value={searchQuery}
                                        onChange={(e) => setSearchQuery(e.target.value)}
                                        placeholder="按文件名搜索文档..."
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
                                <span className="text-xs text-[#6e6b63] dark:text-[#a19f96]">
                                    显示 {filteredDocs.length} / {totalDocs} 份文档
                                </span>
                            </div>
                            <div className="divide-y divide-[#e6e2d8]/60 dark:divide-[#282724]/60">
                                {filteredDocs.map((doc) => (
                                    <div
                                        key={doc.id}
                                        className="p-4 flex items-center justify-between hover:bg-[#f3f0e6]/50 dark:hover:bg-[#22211e] transition-colors"
                                    >
                                        <div className="flex items-center gap-3 min-w-0">
                                            <div className="w-9 h-9 rounded-xl bg-[#da7756]/10 text-[#da7756] flex items-center justify-center shrink-0">
                                                <FileText className="w-4 h-4" />
                                            </div>
                                            <div className="min-w-0">
                                                <h4 className="text-xs font-medium text-[#1f1e1d] dark:text-[#edece8] truncate">
                                                    {doc.name}
                                                </h4>
                                                <span className="text-[10px] text-[#918d83]">
                                                    {formatSize(doc.size)}
                                                    {doc.createdAt
                                                        ? ` · ${new Date(doc.createdAt).toLocaleDateString()}`
                                                        : ""}
                                                </span>
                                            </div>
                                        </div>
                                        <div className="flex items-center gap-4 text-xs shrink-0">
                                            <span className="text-[#6e6b63] dark:text-[#a19f96] font-mono">
                                                {doc.chunks} 块
                                            </span>
                                            <span
                                                className={`px-2.5 py-1 rounded-lg font-medium ${
                                                    doc.status === "indexed"
                                                        ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                                                        : doc.status === "processing"
                                                          ? "bg-amber-500/10 text-amber-600 dark:text-amber-400"
                                                          : "bg-rose-500/10 text-rose-600 dark:text-rose-400"
                                                }`}
                                            >
                                                {DOCUMENT_STATUS_LABELS[doc.status]}
                                            </span>
                                            <button
                                                onClick={() => handleDelete(doc.id)}
                                                disabled={deletingId === doc.id}
                                                title="删除文档"
                                                className="p-1.5 rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:text-rose-600 hover:bg-rose-500/10 transition-colors disabled:opacity-50"
                                            >
                                                {deletingId === doc.id ? (
                                                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                                ) : (
                                                    <Trash2 className="w-3.5 h-3.5" />
                                                )}
                                            </button>
                                        </div>
                                    </div>
                                ))}
                                {filteredDocs.length === 0 && !loading && (
                                    <div className="p-14 text-center">
                                        <FileText className="w-8 h-8 text-[#dcd7cb] dark:text-[#33312d] mx-auto mb-3" />
                                        <p className="text-xs text-[#918d83]">
                                            {searchQuery
                                                ? "未找到匹配的文档。"
                                                : "库是空的。拖一份文档进来，下一轮对话就能引用。"}
                                        </p>
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                ) : (
                    <RetrievalDebugger />
                )}
            </div>
        </div>
    );
};

const StatCard: React.FC<{
    label: string;
    value: string;
    icon: React.ReactNode;
    accent?: boolean;
    ok?: boolean;
}> = ({ label, value, icon, accent, ok }) => (
    <div className="card-surface card-lift rounded-2xl p-5 space-y-2">
        <div className="flex items-center gap-1.5 text-[10px] font-semibold tracking-wide text-[#6e6b63] dark:text-[#a19f96]">
            <span className="text-[#da7756]">{icon}</span>
            {label}
        </div>
        <div
            className={`text-2xl font-bold ${
                ok
                    ? "text-emerald-600 dark:text-emerald-400"
                    : accent
                      ? "text-[#da7756]"
                      : "text-[#1f1e1d] dark:text-[#edece8]"
            }`}
            style={{ fontFamily: "var(--font-mono)" }}
        >
            {value}
        </div>
    </div>
);
