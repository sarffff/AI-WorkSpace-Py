import React, { useEffect, useState } from "react";
import { apiClient } from "@/shared/api/client";
import type { KnowledgeDocument } from "@/shared/types/api.types";
import { Database, Upload, FileText, Search, ShieldCheck } from "lucide-react";

function formatSize(bytes: number): string {
    if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
    if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(0)} KB`;
    return `${bytes} B`;
}

export const KnowledgePage: React.FC = () => {
    const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
    const [totalDocs, setTotalDocs] = useState(0);
    const [totalChunks, setTotalChunks] = useState(0);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
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
            .finally(() => setLoading(false));
    }, []);

    return (
        <div className="p-8 h-full overflow-y-auto space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h3 className="text-lg font-semibold text-slate-100">
                        Knowledge Base & RAG
                    </h3>
                    <p className="text-xs text-slate-400 mt-1">
                        Manage documents, embeddings, and vector database
                        indices.
                    </p>
                </div>
                <button className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium rounded-lg flex items-center gap-2 shadow-lg shadow-indigo-600/20 transition-all">
                    <Upload className="w-4 h-4" />
                    Upload Document
                </button>
            </div>

            <div className="grid grid-cols-3 gap-4">
                <div className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 space-y-1">
                    <span className="text-xs text-slate-400">
                        Total Documents
                    </span>
                    <div className="text-2xl font-bold text-slate-100">
                        {loading ? "..." : totalDocs}
                    </div>
                </div>
                <div className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 space-y-1">
                    <span className="text-xs text-slate-400">
                        Vector Embeddings
                    </span>
                    <div className="text-2xl font-bold text-indigo-400">
                        {loading ? "..." : totalChunks}
                    </div>
                </div>
                <div className="p-4 rounded-xl bg-slate-900/60 border border-slate-800 space-y-1">
                    <span className="text-xs text-slate-400">
                        Database Status
                    </span>
                    <div className="text-2xl font-bold text-emerald-400 flex items-center gap-2">
                        <ShieldCheck className="w-5 h-5" /> MySQL + Redis
                    </div>
                </div>
            </div>

            <div className="rounded-xl bg-slate-900/60 border border-slate-800 overflow-hidden">
                <div className="p-4 border-b border-slate-800 flex items-center justify-between">
                    <div className="flex items-center gap-2 bg-slate-950 px-3 py-1.5 rounded-lg border border-slate-800 w-72">
                        <Search className="w-4 h-4 text-slate-500" />
                        <input
                            type="text"
                            placeholder="Search documents..."
                            className="bg-transparent border-none text-xs text-slate-200 placeholder-slate-500 focus:outline-none w-full"
                        />
                    </div>
                    <span className="text-xs text-slate-400">
                        Showing {documents.length} of {totalDocs} documents
                    </span>
                </div>
                <div className="divide-y divide-slate-800/80">
                    {documents.map((doc) => (
                        <div
                            key={doc.id}
                            className="p-4 flex items-center justify-between hover:bg-slate-800/30 transition-colors"
                        >
                            <div className="flex items-center gap-3">
                                <div className="w-9 h-9 rounded-lg bg-indigo-500/10 text-indigo-400 flex items-center justify-center">
                                    <FileText className="w-4 h-4" />
                                </div>
                                <div>
                                    <h4 className="text-xs font-medium text-slate-200">
                                        {doc.name}
                                    </h4>
                                    <span className="text-[10px] text-slate-500">
                                        Size: {formatSize(doc.size)}
                                    </span>
                                </div>
                            </div>
                            <div className="flex items-center gap-4 text-xs">
                                <span className="text-slate-400">
                                    {doc.chunks} chunks
                                </span>
                                <span className="text-emerald-400 px-2 py-0.5 rounded bg-emerald-500/10 font-medium capitalize">
                                    {doc.status}
                                </span>
                            </div>
                        </div>
                    ))}
                    {documents.length === 0 && !loading && (
                        <div className="p-8 text-center text-xs text-slate-500">
                            No documents found. Upload your first document.
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};
