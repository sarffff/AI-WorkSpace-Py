import React from "react";
import { TIER_HINTS, tierLabel } from "@/shared/lib/format";

interface TierMarkProps {
  /** read | mutate | fund。未登记的值按最重的一档着色，宁重不轻 */
  tier: string | null | undefined;
  /** 短标签 + 一句解释。审批卡片上要用带解释的那一版 */
  withHint?: boolean;
}

/**
 * 权限档位的标记。
 *
 * 颜色不是装饰：`fund` 那一档意味着"这一步不会自己执行，要有人点头"，
 * 所以它必须在还没读到说明之前就把人拦住。三档的颜色都来自 token，
 * 深色模式下整体提亮一档——低对比的信号色在暗背景上等于没有。
 */
export const TierMark: React.FC<TierMarkProps> = ({ tier, withHint }) => {
  const known = tier === "read" || tier === "mutate" || tier === "fund";
  const tone = known ? tier : "fund";
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px]" data-tier={tone}>
      <span
        aria-hidden="true"
        className="w-1.5 h-1.5 rounded-full"
        style={{ background: `var(--tier-${tone})` }}
      />
      <span className="font-semibold text-ink-soft">{tierLabel(tier)}</span>
      {withHint && (
        <span className="text-ink-faint">· {TIER_HINTS[tone]}</span>
      )}
    </span>
  );
};
