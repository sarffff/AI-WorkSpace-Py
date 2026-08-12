import React, { useEffect, useRef, useState } from "react";
import { apiClient } from "@/shared/api/client";
import type { KnowledgeDocument } from "@/shared/types/api.types";
import {
    Upload,
    FileText,
    Search,
    ShieldCheck,
    Trash2,
    Loader2,
    X,
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
    const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
    const [totalDocs, setTotalDocs] = useState(0);
    const [totalChunks, setTotalChunks] = useState(0);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(false);
    const [searchQuery, setSearchQuery] = useState("");
    const [deletingId, setDeletingId] = useState<string | null>(null);
    const [errorMsg, setErrorMsg] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const refreshDocuments = (showSpinner = true) => {
        if (showSpinner) setLoading(true);
        apiClient
            .getDocuments()
            .then((docs) => {
                setDocuments(docs);
                setTotalDocs(docs.length);
                setTotalChunks(docs.reduce((sum, d) => sum + d.chunks, 0));
            })
            .catch(() => {
                // fallback to empty
            })
            .finally(() => {
                if (showSpinner) setLoading(false);
            });
    };

    useEffect(() => {
        refreshDocuments();
    }, []);

    // 分块与向量化在后台进行，上传接口只返回 processing，需要轮询到终态
    const hasProcessing = documents.some((doc) => doc.status === "processing");
    useEffect(() => {
        if (!hasProcessing) return;
        const timer = setInterval(() => refreshDocuments(false), 2000);
        return () => clearInterval(timer);
    }, [hasProcessing]);

    const handleUploadClick = () => {
        fileInputRef.current?.click();
    };

    const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;
        // 重置 input，便于再次选择同一文件
        e.target.value = "";

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

    // 客户端按名称过滤（简单且即时）
    const filteredDocs = searchQuery.trim()
        ? documents.filter((d) =>
              d.name.toLowerCase().includes(searchQuery.trim().toLowerCase()),
          )
        : documents;

    return (
        <div className="p-8 h-full overflow-y-auto space-y-6 bg-[#fbf9f5] dark:bg-[#141413] transition-colors duration-200">
            <div className="flex items-center justify-between">
                <div>
                    <h3 className="text-lg font-semibold text-[#1f1e1d] dark:text-[#edece8]">
                        知识库与 RAG
                    </h3>
                    <p className="text-xs text-[#6e6b63] dark:text-[#a19f96] mt-1">
                        管理本地文档、向量索引与检索状态，让回答能够引用你的资料。
                    </p>
                </div>
                <button
                    onClick={handleUploadClick}
                    disabled={uploading}
                    className="px-4 py-2.5 bg-[#da7756] hover:bg-[#c86544] disabled:opacity-60 disabled:cursor-not-allowed text-white text-xs font-medium rounded-xl flex items-center gap-2 shadow-md shadow-[#da7756]/20 transition-all"
                >
                    {uploading ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                        <Upload className="w-4 h-4" />
                    )}
                    {uploading ? "上传中..." : "上传文档"}
                </button>
                <input
                    ref={fileInputRef}
                    type="file"
                    className="hidden"
                    accept=".txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.xml,.yaml,.yml,.css,.html,.pdf"
                    onChange={handleFileChange}
                />
            </div>

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

            <div className="grid grid-cols-3 gap-4">
                <div className="p-5 rounded-2xl bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] space-y-1 shadow-sm">
                    <span className="text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96]">
                        文档总数
                    </span>
                    <div className="text-2xl font-bold text-[#1f1e1d] dark:text-[#edece8]">
                        {loading ? "..." : totalDocs}
                    </div>
                </div>
                <div className="p-5 rounded-2xl bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] space-y-1 shadow-sm">
                    <span className="text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96]">
                        向量分块
                    </span>
                    <div className="text-2xl font-bold text-[#da7756]">
                        {loading ? "..." : totalChunks}
                    </div>
                </div>
                <div className="p-5 rounded-2xl bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] space-y-1 shadow-sm">
                    <span className="text-xs font-semibold uppercase tracking-wider text-[#6e6b63] dark:text-[#a19f96]">
                        数据库状态
                    </span>
                    <div className="text-2xl font-bold text-emerald-600 dark:text-emerald-400 flex items-center gap-2">
                        <ShieldCheck className="w-5 h-5" /> MySQL 正常
                    </div>
                </div>
            </div>

            <div className="rounded-2xl bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] overflow-hidden shadow-sm">
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
                            <div className="flex items-center gap-3">
                                <div className="w-9 h-9 rounded-xl bg-[#da7756]/10 text-[#da7756] flex items-center justify-center">
                                    <FileText className="w-4 h-4" />
                                </div>
                                <div>
                                    <h4 className="text-xs font-medium text-[#1f1e1d] dark:text-[#edece8]">
                                        {doc.name}
                                    </h4>
                                    <span className="text-[10px] text-[#918d83]">
                                        文件大小：{formatSize(doc.size)}
                                    </span>
                                </div>
                            </div>
                            <div className="flex items-center gap-4 text-xs">
                                <span className="text-[#6e6b63] dark:text-[#a19f96]">
                                    {doc.chunks} 个分块
                                </span>
                                <span
                                    className={`px-2.5 py-1 rounded-lg bg-emerald-500/10 font-medium capitalize ${
                                        doc.status === "indexed"
                                            ? "text-emerald-600 dark:text-emerald-400"
                                            : doc.status === "processing"
                                              ? "text-amber-600 dark:text-amber-400"
                                              : "text-rose-600 dark:text-rose-400"
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
                        <div className="p-12 text-center text-xs text-[#918d83]">
                            {searchQuery
                                ? "未找到匹配的文档。"
                                : "暂无文档，点击右上角上传你的第一份文档。"}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};
