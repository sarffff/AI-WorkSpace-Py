import React, { useState } from "react";
import { apiClient } from "@/shared/api/client";
import type { KnowledgeQueryChunk } from "@/shared/types/api.types";
import { Search, Loader2, X, ChevronDown, ChevronUp } from "lucide-react";

export const RetrievalDebugger: React.FC = () => {
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [results, setResults] = useState<KnowledgeQueryChunk[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const handleSearch = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await apiClient.queryKnowledge(query.trim(), topK);
      setResults(res.results);
    } catch (e) {
      setError(e instanceof Error ? e.message : "检索失败");
    } finally {
      setLoading(false);
    }
  };

  const toggleExpand = (idx: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  return (
    <div className="space-y-4">
      {/* 搜索栏 */}
      <div className="flex items-center gap-2">
        <div className="flex-1 flex items-center gap-2 bg-white dark:bg-[#1a1917] border border-[#e6e2d8] dark:border-[#282724] px-3.5 py-2 rounded-xl focus-within:border-[#da7756]/50 transition-colors">
          <Search className="w-4 h-4 text-[#918d83] shrink-0" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="输入测试检索问题..."
            className="flex-1 bg-transparent border-none text-xs text-[#1f1e1d] dark:text-[#edece8] placeholder-[#918d83] focus:outline-none"
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              className="text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
        <select
          value={topK}
          onChange={(e) => setTopK(Number(e.target.value))}
          className="bg-[#f3f0e6] dark:bg-[#201f1c] border border-[#e3dfd5] dark:border-[#2e2d2a] rounded-xl px-2.5 py-2 text-xs text-[#1f1e1d] dark:text-[#edece8] focus:outline-none"
        >
          {[1, 3, 5, 10, 20].map((k) => (
            <option key={k} value={k}>
              top {k}
            </option>
          ))}
        </select>
        <button
          onClick={handleSearch}
          disabled={loading || !query.trim()}
          className="btn-accent px-4 py-2 rounded-xl text-xs font-medium flex items-center gap-2 disabled:opacity-50"
        >
          {loading ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <Search className="w-3.5 h-3.5" />
          )}
          检索
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs">
          <X className="w-4 h-4 shrink-0" />
          {error}
        </div>
      )}

      {loading && (
        <div className="flex items-center gap-2 text-xs text-[#6e6b63] dark:text-[#a19f96]">
          <Loader2 className="w-3.5 h-3.5 animate-spin" /> 正在检索...
        </div>
      )}

      {!loading && results.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs text-[#6e6b63] dark:text-[#a19f96]">
            结果 {results.length} 条
          </div>
          {results.map((chunk, idx) => (
            <div
              key={`${chunk.document_id}-${chunk.chunk_index}-${idx}`}
              className="card-surface rounded-xl p-3.5 space-y-2"
            >
              <div className="flex items-center gap-2 text-xs">
                <span className="w-5 h-5 rounded bg-[#da7756]/12 text-[#da7756] text-[10px] font-semibold flex items-center justify-center shrink-0">
                  {idx + 1}
                </span>
                <span className="font-medium text-[#1f1e1d] dark:text-[#edece8]">
                  {chunk.document_name}
                </span>
                <span className="text-[#918d83]">
                  分块 {chunk.chunk_index}
                  {chunk.chunk_range?.[0] !== undefined &&
                    `(${chunk.chunk_range[0]}-${chunk.chunk_range[1]})`}
                </span>
                <span className="ml-auto flex items-center gap-1.5">
                  {chunk.channels?.map((ch) => (
                    <span
                      key={ch}
                      className="chip chip-accent text-[9px] px-1.5 py-0.5"
                    >
                      {ch}
                    </span>
                  ))}
                </span>
              </div>
              <div className="flex items-center gap-3 text-[11px] text-[#6e6b63] dark:text-[#a19f96]">
                <span>
                  稠密得分:{" "}
                  <span
                    className="font-mono text-[#1f1e1d] dark:text-[#edece8]"
                    style={{ fontFamily: "var(--font-mono)" }}
                  >
                    {chunk.score?.toFixed(4) ?? "-"}
                  </span>
                </span>
                {chunk.fusion_score !== null && chunk.fusion_score !== undefined && (
                  <span>
                    fusion:{" "}
                    <span
                      className="font-mono text-[#1f1e1d] dark:text-[#edece8]"
                      style={{ fontFamily: "var(--font-mono)" }}
                    >
                      {chunk.fusion_score.toFixed(4)}
                    </span>
                  </span>
                )}
              </div>
              <button
                onClick={() => toggleExpand(idx)}
                className="flex items-center gap-1 text-[10px] text-[#da7756] hover:underline"
              >
                {expanded.has(idx) ? (
                  <ChevronUp className="w-3 h-3" />
                ) : (
                  <ChevronDown className="w-3 h-3" />
                )}
                {expanded.has(idx) ? "收起" : "展开"}全文
              </button>
              {expanded.has(idx) && (
                <div
                  className="text-[11px] text-[#1f1e1d] dark:text-[#edece8] bg-[#faf9f5] dark:bg-[#191817] rounded-lg p-3 leading-relaxed"
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  {chunk.content}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {!loading && results.length === 0 && query && (
        <div className="card-surface rounded-2xl p-12 text-center">
          <p className="text-xs text-[#918d83]">
            未检索到相关结果。换个说法，或先上传文档。
          </p>
        </div>
      )}

      {!loading && results.length === 0 && !query && (
        <div className="card-surface rounded-2xl p-10 text-center space-y-2">
          <p className="text-sm font-medium text-[#1f1e1d] dark:text-[#edece8]">
            不经过对话，直接看检索器
          </p>
          <p className="text-[11px] text-[#918d83] max-w-sm mx-auto leading-relaxed">
            输入一个问题。结果会标出 dense / sparse 通道和 fusion 得分——这是核对「它为什么引用这段」的地方。
          </p>
        </div>
      )}
    </div>
  );
};