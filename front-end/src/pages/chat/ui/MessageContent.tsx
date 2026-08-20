import React, { memo } from "react";
import { RagMarker } from "../components/RagMarker";
import { MarkdownBlock } from "../components/MarkdownBlock";

/**
 * 渲染 AI 回复的 Markdown 内容：
 * - 支持 GFM（表格、删除线、任务列表等）
 * - 代码块 highlight.js 高亮 + 一键复制
 * - 保留原 RAG 系统提示行的特殊样式
 */
const MessageContentBase: React.FC<{ content: string }> = ({ content }) => {
    if (!content) return null;

    // 把 RAG 系统提示行单独抽出来，避免被 markdown 误格式化
    const segments = splitRagMarkers(content);

    return (
        <div className="text-sm leading-relaxed text-[#1f1e1d] dark:text-[#edece8] space-y-3">
            {segments.map((seg, i) =>
                seg.type === "rag" ? (
                    <RagMarker key={i} text={seg.text} />
                ) : (
                    <MarkdownBlock key={i} text={seg.text} />
                ),
            )}
        </div>
    );
};

export const MessageContent = memo(MessageContentBase);

/** 把 RAG 系统提示行与普通内容分离 */
function splitRagMarkers(
    content: string,
): Array<{ type: "rag" | "md"; text: string }> {
    const lines = content.split("\n");
    const result: Array<{ type: "rag" | "md"; text: string }> = [];
    let buf: string[] = [];

    const flush = () => {
        if (buf.length > 0) {
            result.push({ type: "md", text: buf.join("\n") });
            buf = [];
        }
    };

    for (const line of lines) {
        const t = line.trim();
        if (
            /^🔍 \*\*\[系统：正在检索知识库 - 搜索词: ".*"\]\*\*$/.test(t) ||
            /^✅ \*\*\[系统：知识库检索完成，正在生成解答...\]\*\*$/.test(t)
        ) {
            flush();
            result.push({ type: "rag", text: t });
        } else {
            buf.push(line);
        }
    }
    flush();
    return result;
}
