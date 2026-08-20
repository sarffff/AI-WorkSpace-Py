import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { CodeBlock } from "./CodeBlock";

export const MarkdownBlock: React.FC<{ text: string }> = ({ text }) => {
    return (
        <div className="markdown-body">
            <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
                components={{
                    code: CodeBlock as never,
                    pre: ({ children }) => <>{children}</>,
                    a: ({ node: _node, ...props }) => (
                        <a
                            {...props}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-[#da7756] underline hover:opacity-80"
                        />
                    ),
                    table: ({ node: _node, ...props }) => (
                        <div className="overflow-x-auto my-3">
                            <table
                                {...props}
                                className="min-w-full text-xs border border-[#e6e2d8] dark:border-[#282724] rounded-lg overflow-hidden"
                            />
                        </div>
                    ),
                    th: ({ node: _node, ...props }) => (
                        <th
                            {...props}
                            className="px-3 py-2 bg-[#f3f0e6] dark:bg-[#201f1c] border border-[#e6e2d8] dark:border-[#282724] text-left font-semibold"
                        />
                    ),
                    td: ({ node: _node, ...props }) => (
                        <td
                            {...props}
                            className="px-3 py-2 border border-[#e6e2d8] dark:border-[#282724]"
                        />
                    ),
                    ul: ({ node: _node, ...props }) => (
                        <ul {...props} className="list-disc pl-5 my-2 space-y-1" />
                    ),
                    ol: ({ node: _node, ...props }) => (
                        <ol {...props} className="list-decimal pl-5 my-2 space-y-1" />
                    ),
                    blockquote: ({ node: _node, ...props }) => (
                        <blockquote
                            {...props}
                            className="border-l-[3px] border-[#da7756]/60 bg-[#da7756]/5 rounded-r-lg py-1.5 px-3 my-2 text-[#6e6b63] dark:text-[#a19f96] italic"
                        />
                    ),
                    h1: ({ node: _node, ...props }) => (
                        <h1
                            {...props}
                            className="font-display text-lg font-semibold mt-4 mb-2.5"
                        />
                    ),
                    h2: ({ node: _node, ...props }) => (
                        <h2
                            {...props}
                            className="font-display text-base font-semibold mt-3.5 mb-2"
                        />
                    ),
                    h3: ({ node: _node, ...props }) => (
                        <h3
                            {...props}
                            className="text-sm font-semibold mt-3 mb-1.5"
                        />
                    ),
                }}
            >
                {text}
            </ReactMarkdown>
        </div>
    );
};
