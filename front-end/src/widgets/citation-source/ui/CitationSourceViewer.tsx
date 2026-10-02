import React, { useEffect, useState } from "react";
import { BookOpen, Loader2, X } from "lucide-react";
import { apiClient } from "@/shared/api/client";
import type { Citation, DocumentChunkView } from "@/shared/types/api.types";

/**
 * 引用来源查看器：点一条 RAG 引用，把命中分块连同相邻上下文拉回来看（B5）。
 *
 * 改之前引用只是一行带 `title` 悬浮提示的静态文字——鼠标移上去才看得到前 400 字，
 * 看不到前后文、也没法确认"这句结论到底对不对得上原文"。这里调
 * `/knowledge/documents/{id}/chunks/{idx}?window=1` 取命中块 + 邻域，命中块高亮。
 *
 * 后端按**当前用户的检索可见范围**收口（共享 + 自己的私有）：引用来自用户自己那次
 * 回答，这里只是让他回看，读不到别人的私有文档（不可见即 404）。所以这里不需要再判权限。
 */
export const CitationSourceViewer: React.FC<{
  citation: Citation;
  onClose: () => void;
}> = ({ citation, onClose }) => {
  const [view, setView] = useState<DocumentChunkView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    apiClient
      .getDocumentChunk(citation.document_id, citation.chunk_index, 1)
      .then((res) => {
        if (alive) setView(res);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "来源加载失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [citation.document_id, citation.chunk_index]);

  // Esc 关闭：模态打开时最顺手的退出
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="引用来源"
    >
      <div
        className="absolute inset-0 bg-black/30 backdrop-blur-sm"
        onClick={onClose}
      />
      <div className="relative card-surface rounded-2xl w-full max-w-2xl max-h-[80vh] flex flex-col anim-fade-up overflow-hidden">
        <header className="flex items-start justify-between gap-3 px-5 py-4 border-b border-[#e6e2d8] dark:border-[#282724]">
          <div className="min-w-0">
            <div className="label-eyebrow leading-none mb-1">参考来源</div>
            <div className="flex items-center gap-2 min-w-0">
              <BookOpen className="w-4 h-4 shrink-0 text-[#da7756]" />
              <h3 className="font-display text-[15px] font-semibold text-[#1f1e1d] dark:text-[#edece8] truncate">
                {view?.documentName || citation.document_name}
              </h3>
            </div>
            <div className="mt-1.5 flex items-center gap-1.5 flex-wrap">
              <span className="chip text-[9px]">
                分块 #{citation.chunk_index}
              </span>
              {typeof citation.score === "number" && (
                <span className="chip text-[9px]">
                  相似度 {citation.score.toFixed(2)}
                </span>
              )}
              {!!citation.channels?.length && (
                <span className="chip text-[9px]">
                  {citation.channels.join(" + ")}
                </span>
              )}
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="p-1 rounded-lg text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] transition-colors shrink-0"
          >
            <X className="w-4 h-4" />
          </button>
        </header>

        <div className="overflow-y-auto px-5 py-4 space-y-3">
          {loading && (
            <div className="flex items-center justify-center gap-2 py-12 text-xs text-[#6e6b63] dark:text-[#a19f96]">
              <Loader2 className="w-4 h-4 animate-spin" /> 正在取回原文...
            </div>
          )}
          {error && !loading && (
            <div className="rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-600 dark:text-rose-400 text-xs px-3 py-2.5">
              {error}
            </div>
          )}
          {!loading &&
            !error &&
            view?.chunks.map((chunk) => {
              const isHit = chunk.chunkIndex === citation.chunk_index;
              return (
                <div
                  key={chunk.chunkIndex}
                  className={`rounded-xl border px-3.5 py-3 ${
                    isHit
                      ? "border-[#da7756]/40 bg-[#da7756]/[0.06]"
                      : "border-[#e6e2d8] dark:border-[#282724] bg-[#faf9f5] dark:bg-[#191817] opacity-80"
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="label-eyebrow leading-none">
                      分块 #{chunk.chunkIndex}
                    </span>
                    {isHit && (
                      <span className="chip chip-accent text-[9px]">命中</span>
                    )}
                  </div>
                  <p className="text-[13px] leading-relaxed text-[#1f1e1d] dark:text-[#edece8] whitespace-pre-wrap break-words">
                    {chunk.content}
                  </p>
                </div>
              );
            })}
          {!loading && !error && view && view.chunks.length === 0 && (
            <p className="py-10 text-center text-xs text-[#918d83]">
              该分块已不在当前库中。
            </p>
          )}
        </div>
      </div>
    </div>
  );
};
