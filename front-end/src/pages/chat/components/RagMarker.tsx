import React from "react";

export const RagMarker: React.FC<{ text: string }> = ({ text }) => {
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
