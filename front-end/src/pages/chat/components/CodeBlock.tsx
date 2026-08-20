import React, { useState } from "react";
import { Check, Copy } from "lucide-react";

type CodeProps = {
    inline?: boolean;
    className?: string;
    children?: React.ReactNode;
};

function extractLang(className?: string): string {
    if (!className) return "";
    const m = className.match(/language-(\w+)/);
    return m ? m[1] : "";
}

export const CodeBlock: React.FC<CodeProps> = ({ inline, className, children }) => {
    const [copied, setCopied] = useState(false);
    const codeText = String(children ?? "");

    if (inline) {
        return (
            <code
                className={`px-1.5 py-0.5 rounded-md bg-[#da7756]/10 text-[#c86544] dark:text-[#e08a6a] text-[0.85em] border border-[#da7756]/20 ${className ?? ""}`}
                style={{ fontFamily: "var(--font-mono)" }}
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
        <div className="relative group my-3 rounded-xl overflow-hidden border border-[#e3dfd5] dark:border-[#2e2d2a] bg-[var(--hl-code-bg)] shadow-sm">
            <div className="flex items-center justify-between px-3.5 py-2 border-b border-[#e3dfd5]/70 dark:border-[#2e2d2a]">
                <div className="flex items-center gap-2.5">
                    <span className="flex items-center gap-1.5">
                        <span className="w-2.5 h-2.5 rounded-full bg-[#ff5f57]/80" />
                        <span className="w-2.5 h-2.5 rounded-full bg-[#febc2e]/80" />
                        <span className="w-2.5 h-2.5 rounded-full bg-[#28c840]/80" />
                    </span>
                    <span
                        className="text-[10px] text-[#918d83] uppercase tracking-wider"
                        style={{ fontFamily: "var(--font-mono)" }}
                    >
                        {extractLang(className) || "code"}
                    </span>
                </div>
                <button
                    onClick={handleCopy}
                    className={`flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-md transition-all ${
                        copied
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-[#918d83] hover:text-[#da7756] hover:bg-[#da7756]/10"
                    }`}
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
            <pre className="overflow-x-auto p-3.5 text-xs leading-relaxed">
                <code
                    className={className ?? ""}
                    style={{ fontFamily: "var(--font-mono)" }}
                >
                    {children}
                </code>
            </pre>
        </div>
    );
};
