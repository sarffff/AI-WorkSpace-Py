import React, { memo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Check, Copy } from "lucide-react";

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

const RagMarker: React.FC<{ text: string }> = ({ text }) => {
    const searchMatch = text.match(
        /^🔍 \*\*\[系统：正在检索知识库 - 搜索词: "(.*)"\]\*\*$/,
    );
    if (searchMatch) {
        return (
            <div className="my-2 p-3 rounded-xl bg-[#da7756]/10 border border-[#da7756]/20 text-[#da7756] flex items-center gap-2.5 text-xs font-medium">
                <span className="animate-pulse">🔍</span>
                <span>
                    正在智能检索本地知识库，搜索词：
                    <strong>“{searchMatch[1]}”</strong>...
                </span>
            </div>
        );
    }
    return (
        <div className="my-2 p-3 rounded-xl bg-emerald-500/10 border border-emerald-500/20 text-emerald-600 dark:text-emerald-400 flex items-center gap-2.5 text-xs font-medium">
            <span>✅</span>
            <span>知识库检索完成，正在生成解答...</span>
        </div>
    );
};

const MarkdownBlock: React.FC<{ text: string }> = ({ text }) => {
    return (
        <div className="markdown-body">
            <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
                components={{
                    code: CodeBlock as never,
                    pre: ({ children }) => <>{children}</>,
                    a: ({ node, ...props }) => (
                        <a
                            {...props}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-[#da7756] underline hover:opacity-80"
                        />
                    ),
                    table: ({ node, ...props }) => (
                        <div className="overflow-x-auto my-3">
                            <table
                                {...props}
                                className="min-w-full text-xs border border-[#e6e2d8] dark:border-[#282724] rounded-lg overflow-hidden"
                            />
                        </div>
                    ),
                    th: ({ node, ...props }) => (
                        <th
                            {...props}
                            className="px-3 py-2 bg-[#f3f0e6] dark:bg-[#201f1c] border border-[#e6e2d8] dark:border-[#282724] text-left font-semibold"
                        />
                    ),
                    td: ({ node, ...props }) => (
                        <td
                            {...props}
                            className="px-3 py-2 border border-[#e6e2d8] dark:border-[#282724]"
                        />
                    ),
                    ul: ({ node, ...props }) => (
                        <ul {...props} className="list-disc pl-5 my-2 space-y-1" />
                    ),
                    ol: ({ node, ...props }) => (
                        <ol {...props} className="list-decimal pl-5 my-2 space-y-1" />
                    ),
                    blockquote: ({ node, ...props }) => (
                        <blockquote
                            {...props}
                            className="border-l-2 border-[#da7756] pl-3 my-2 text-[#6e6b63] dark:text-[#a19f96]"
                        />
                    ),
                    h1: ({ node, ...props }) => (
                        <h1 {...props} className="text-lg font-bold my-3" />
                    ),
                    h2: ({ node, ...props }) => (
                        <h2 {...props} className="text-base font-bold my-2" />
                    ),
                    h3: ({ node, ...props }) => (
                        <h3 {...props} className="text-sm font-bold my-2" />
                    ),
                }}
            >
                {text}
            </ReactMarkdown>
        </div>
    );
};

type CodeProps = {
    inline?: boolean;
    className?: string;
    children?: React.ReactNode;
};

const CodeBlock: React.FC<CodeProps> = ({ inline, className, children }) => {
    const [copied, setCopied] = useState(false);
    const codeText = String(children ?? "");

    if (inline) {
        return (
            <code
                className={`px-1.5 py-0.5 rounded bg-[#f3f0e6] dark:bg-[#201f1c] text-[#da7756] text-[0.85em] font-mono ${className ?? ""}`}
            >
                {children}
            </code>
        );
    }

    const handleCopy = () => {
        navigator.clipboard.writeText(codeText).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        });
    };

    return (
        <div className="relative group my-3 rounded-xl overflow-hidden border border-[#282724] bg-[#1a1917]">
            <div className="flex items-center justify-between px-3 py-1.5 bg-[#201f1c] border-b border-[#282724]">
                <span className="text-[10px] text-[#918d83] font-mono">
                    {extractLang(className)}
                </span>
                <button
                    onClick={handleCopy}
                    className="flex items-center gap-1 text-[10px] text-[#918d83] hover:text-[#edece8] transition-colors"
                >
                    {copied ? (
                        <>
                            <Check className="w-3 h-3" /> 已复制
                        </>
                    ) : (
                        <>
                            <Copy className="w-3 h-3" /> 复制
                        </>
                    )}
                </button>
            </div>
            <pre className="overflow-x-auto p-3 text-xs leading-relaxed">
                <code className={`font-mono ${className ?? ""}`}>{children}</code>
            </pre>
        </div>
    );
};

function extractLang(className?: string): string {
    if (!className) return "";
    const m = className.match(/language-(\w+)/);
    return m ? m[1] : "";
}
