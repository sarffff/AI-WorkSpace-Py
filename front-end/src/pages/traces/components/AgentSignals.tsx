import React from "react";

/**
 * Agent 回合信号：把 span attributes 里那些"要盯的东西"抬成命名行。
 *
 * 为什么单独做一层而不是继续看原始 JSON：AGENT_STATUS 的开关表里每一项都有一列
 * "要盯的东西"，而那些值全埋在一个 JSON 字符串里——要判断 skill 到底有没有生效，
 * 得先点进 trace、找对 span、再在一坨 snake_case 里认出 `skills_loaded`。
 * 这一层只做"让它可读"，不改任何数据。
 *
 * **不认识的键必须继续显示。** 这是这个文件唯一的硬约束：后端 `turn.set()` 加一个
 * 新键是很随意的动作（这一版就加了 `skills_loaded`、`budget_reclaimed` 两个），
 * 如果这里只渲染白名单，新键会**静默消失**——UI 看着很整齐，实际在骗人。
 * 所以未识别的键一律交回上层原样打印，见 `partitionAttributes`。
 */

/** 一条命名信号的渲染规则 */
interface SignalSpec {
  label: string;
  /** 值的呈现；返回 null 表示"这条不值得占一行" */
  render: (value: unknown) => React.ReactNode | null;
  /** 值本身就是坏消息时给个警示色 */
  warn?: (value: unknown) => boolean;
  /** 补一句这个数字意味着什么，只在 warn 命中时显示 */
  hint?: string;
}

const list = (value: unknown): string =>
  Array.isArray(value) ? value.map(String).join("、") : String(value);

const yesNo = (value: unknown): string => (value ? "是" : "否");

/**
 * 认识的键。顺序即展示顺序：先是这一版新加的两个（最需要被看见），
 * 然后按"回合本身 → 中断 → 缓存 → 委派 → 恢复 → 其它"排。
 */
const SIGNALS: Record<string, SignalSpec> = {
  skills_loaded: {
    label: "已加载 skill",
    // 空数组和"键不存在"是两件完全不同的事，见文件头注释。
    // 空数组要显眼：索引每轮都注入进去了，模型一个都没调。
    render: (v) => (Array.isArray(v) && v.length === 0 ? "无" : list(v)),
    warn: (v) => Array.isArray(v) && v.length === 0,
    hint: "索引注入了但一个都没调——先看下面有没有「让路给 skill」，没有的话就是预检索挡住了",
  },
  skill_preempted: {
    label: "让路给 skill",
    // 这一轮为了让模型自己去读索引而没做预检索。和 skills_loaded 一起看：
    // 让了路却还是没加载，才是真的失效；没让路而没加载，是预检索挡住了。
    render: (v) => String(v),
  },
  skill_similarity: {
    label: "相关度",
    render: (v) => String(v),
  },
  budget_reclaimed: {
    label: "预算回收",
    render: (v) => `${Number(v).toLocaleString()} 字符`,
  },
  rounds: {
    label: "工具轮次",
    render: (v) => String(v),
  },
  plan_steps: {
    label: "计划步数",
    render: (v) => String(v),
  },
  tool_history_steps: {
    label: "轨迹回灌",
    render: (v) => `${v} 步`,
  },
  clarification: {
    label: "触发澄清",
    render: yesNo,
  },
  interrupted: {
    label: "中断",
    render: (v) => INTERRUPT_LABELS[String(v)] ?? String(v),
    warn: () => true,
  },
  interrupt_tool: {
    label: "中断于工具",
    render: (v) => String(v),
  },
  repeated_blocked: {
    label: "重复调用被拦",
    render: (v) => `${v} 次`,
    warn: (v) => Number(v) > 0,
    hint: "模型在原地打转，RepeatGuard 挡下了",
  },
  cache_hit: {
    label: "语义缓存",
    render: (v) => (v ? "命中" : "未命中"),
  },
  cache_exact: {
    label: "精确命中",
    render: yesNo,
  },
  cache_similarity: {
    label: "相似度",
    render: (v) => String(v),
  },
  delegation_mode: {
    label: "委派模式",
    render: (v) => String(v),
  },
  delegation_roles: {
    label: "可委派角色",
    render: list,
  },
  replayed_writes: {
    label: "重放写操作",
    render: (v) => `${v} 次`,
  },
  resumed_round: {
    label: "从第几轮恢复",
    render: (v) => String(v),
  },
  run_id: {
    label: "run",
    render: (v) => String(v).slice(0, 8),
  },
  vision_images: {
    label: "识图",
    render: (v) => `${Array.isArray(v) ? v.length : v} 张`,
  },
  vision_skipped: {
    label: "跳过的图",
    render: list,
    warn: (v) => Array.isArray(v) && v.length > 0,
  },
};

const INTERRUPT_LABELS: Record<string, string> = {
  tool_approval: "等待工具审批",
  user_input: "等待用户输入",
  prose_question: "模型用正文提了问",
};

export interface Signal {
  key: string;
  label: string;
  value: React.ReactNode;
  warn: boolean;
  hint?: string;
}

/**
 * 把 attributes 切成"认识的"和"不认识的"两半。
 *
 * 不认识的那半原样交回，由调用方继续用 JSON 打印——白名单式渲染会让后端新加的键
 * 静默消失，那比难读严重得多。
 */
export const partitionAttributes = (
  attributes: Record<string, unknown>,
): { signals: Signal[]; rest: Record<string, unknown> } => {
  const signals: Signal[] = [];
  const rest: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(attributes)) {
    const spec = SIGNALS[key];
    if (!spec) {
      rest[key] = value;
      continue;
    }
    // null/undefined 当作"没这回事"，但 false、0、空数组都是有意义的值。
    if (value === null || value === undefined) {
      continue;
    }
    const rendered = spec.render(value);
    if (rendered === null) {
      rest[key] = value;
      continue;
    }
    const warn = spec.warn?.(value) ?? false;
    signals.push({
      key,
      label: spec.label,
      value: rendered,
      warn,
      hint: warn ? spec.hint : undefined,
    });
  }

  // 按 SIGNALS 的声明顺序排，保证同一个 span 每次看都一样
  const order = Object.keys(SIGNALS);
  signals.sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
  return { signals, rest };
};

export const AgentSignals: React.FC<{ signals: Signal[] }> = ({ signals }) => {
  if (signals.length === 0) return null;
  return (
    <div>
      <div className="text-[10px] font-semibold text-[#6e6b63] dark:text-[#a19f96] mb-1">
        本回合信号
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
        {signals.map((s) => (
          <div key={s.key} className="flex items-center gap-2 min-w-0">
            <span className="text-[#918d83] shrink-0">{s.label}</span>
            <span
              className={`font-medium truncate ${
                s.warn
                  ? "text-amber-600 dark:text-amber-400"
                  : "text-[#1f1e1d] dark:text-[#edece8]"
              }`}
              title={s.hint}
            >
              {s.value}
            </span>
          </div>
        ))}
      </div>
      {signals
        .filter((s) => s.hint)
        .map((s) => (
          <div
            key={`hint-${s.key}`}
            className="mt-1.5 text-[10px] text-amber-600 dark:text-amber-400"
          >
            {s.label}：{s.hint}
          </div>
        ))}
    </div>
  );
};
