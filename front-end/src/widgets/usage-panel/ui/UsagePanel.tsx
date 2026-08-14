import React, { useEffect, useState } from "react";
import { apiClient } from "@/shared/api/client";
import type {
  FeedbackSummary,
  TraceDetail,
  TraceSpanNode,
  TraceSummary,
  UsageGroup,
  UsageSummary,
} from "@/shared/types/api.types";
import { Activity, ChevronRight, Coins, Loader2, Timer } from "lucide-react";
import { fmtCost, fmtInt, fmtMs, spanLabel } from "@/shared/lib/format";

const RANGES = [1, 7, 30] as const;

/** 与后端 services/feedback_service.py 的 REASONS 对齐 */
const REASON_LABELS: Record<string, string> = {
  inaccurate: "内容不准确",
  no_citation: "没引用来源",
  off_topic: "答非所问",
  bad_format: "格式问题",
  other: "其它",
};

export const UsagePanel: React.FC = () => {
  const [days, setDays] = useState<number>(7);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [trace, setTrace] = useState<TraceDetail | null>(null);
  const [feedback, setFeedback] = useState<FeedbackSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([apiClient.getUsage(days), apiClient.getTraces(undefined, 10)])
      .then(([summary, recent]) => {
        if (cancelled) return;
        setUsage(summary);
        setTraces(recent);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "加载用量失败");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    // 满意度是独立数据源，拉不到不该让整个面板报错
    apiClient
      .getFeedbackSummary()
      .then((summary) => {
        if (!cancelled) setFeedback(summary);
      })
      .catch(() => {
        if (!cancelled) setFeedback(null);
      });
    return () => {
      cancelled = true;
    };
  }, [days]);

  const openTrace = (traceId: string) => {
    if (trace?.traceId === traceId) {
      setTrace(null);
      return;
    }
    apiClient
      .getTrace(traceId)
      .then(setTrace)
      .catch(() => setTrace(null));
  };

  return (
    <div className="card-surface rounded-2xl p-5 space-y-5 relative z-10 anim-fade-up stagger-2">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2.5">
          <span className="w-6 h-6 rounded-lg bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
            <Activity className="w-3.5 h-3.5" />
          </span>
          <h3 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
            用量与成本
          </h3>
        </div>
        <div className="flex gap-1">
          {RANGES.map((range) => (
            <button
              key={range}
              onClick={() => setDays(range)}
              className={`px-2.5 py-1 text-[11px] rounded-lg transition-colors ${
                days === range
                  ? "bg-[#da7756] text-white"
                  : "bg-[#f3f0e6] dark:bg-[#262522] text-[#6e6b63] dark:text-[#a19f96] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
              }`}
            >
              {range} 天
            </button>
          ))}
        </div>
      </div>

      {loading && (
        <div className="flex items-center gap-2 text-[11px] text-[#6e6b63] dark:text-[#a19f96]">
          <Loader2 className="w-3.5 h-3.5 animate-spin" /> 正在统计...
        </div>
      )}

      {error && <p className="text-[11px] text-red-500">{error}</p>}

      {usage && !loading && (
        <>
          <div className="grid grid-cols-4 gap-3">
            <Metric label="回答次数" value={fmtInt(usage.totals.turns)} />
            <Metric label="输入 token" value={fmtInt(usage.totals.promptTokens)} />
            <Metric label="输出 token" value={fmtInt(usage.totals.completionTokens)} />
            <Metric
              label="失败片段"
              value={fmtInt(usage.totals.failures)}
              warn={usage.totals.failures > 0}
            />
          </div>

          <div className="flex flex-wrap items-center gap-3 text-[11px]">
            <span className="flex items-center gap-1.5 text-[#6e6b63] dark:text-[#a19f96]">
              <Coins className="w-3.5 h-3.5" />
              {usage.costs.length ? (
                usage.costs.map((entry) => (
                  <span
                    key={entry.currency ?? "unknown"}
                    className="text-[#1f1e1d] dark:text-[#edece8] font-medium"
                  >
                    {fmtCost(entry.amount, entry.currency)}
                  </span>
                ))
              ) : (
                <span>成本未知</span>
              )}
            </span>
            {!usage.pricingConfigured && (
              <span className="text-[#918d83]">
                未配置价目表（复制 model_prices.example.json 为 model_prices.json）
              </span>
            )}
            {usage.totals.estimatedTokenShare !== null &&
              usage.totals.estimatedTokenShare > 0 && (
                <span className="text-[#918d83]">
                  {Math.round(usage.totals.estimatedTokenShare * 100)}% 的 token
                  为本地估算，成本仅供参考
                </span>
              )}
          </div>

          <GroupTable title="按环节" rows={usage.byName} labelOf={(row) => spanLabel(row.name ?? "-")} />
          <GroupTable
            title="按模型"
            rows={usage.byModel.filter((row) => row.model)}
            labelOf={(row) => row.model ?? "-"}
          />

          {usage.cache?.enabled && (
            <div className="space-y-2">
              <h4 className="text-[11px] font-semibold text-[#6e6b63] dark:text-[#a19f96]">
                语义缓存
              </h4>
              <div className="grid grid-cols-4 gap-3">
                <Metric
                  label="命中率"
                  value={
                    usage.cache.hitRate === null
                      ? "-"
                      : `${Math.round(usage.cache.hitRate * 100)}%`
                  }
                />
                <Metric label="命中" value={fmtInt(usage.cache.hits)} />
                <Metric label="未命中" value={fmtInt(usage.cache.misses)} />
                <Metric
                  label="省下 token"
                  value={fmtInt(usage.cache.tokensSaved)}
                />
              </div>
              <p className="text-[11px] text-[#918d83]">
                相似度阈值 {usage.cache.threshold}
                。统计存在服务进程内存里，重启归零，也不随上面的时间窗口变化。
              </p>
            </div>
          )}

          {feedback && feedback.rated > 0 && (
            <div className="space-y-2">
              <h4 className="text-[11px] font-semibold text-[#6e6b63] dark:text-[#a19f96]">
                回答满意度
              </h4>
              <div className="grid grid-cols-4 gap-3">
                <Metric
                  label="满意率"
                  value={
                    feedback.satisfaction === null
                      ? "-"
                      : `${Math.round(feedback.satisfaction * 100)}%`
                  }
                />
                <Metric label="赞" value={fmtInt(feedback.up)} />
                <Metric
                  label="踩"
                  value={fmtInt(feedback.down)}
                  warn={feedback.down > 0}
                />
                <Metric
                  label="待导出用例"
                  value={fmtInt(feedback.pendingExport)}
                />
              </div>
              {feedback.downReasons.length > 0 && (
                <p className="text-[11px] text-[#918d83]">
                  差评原因：
                  {feedback.downReasons
                    .map(
                      (entry) =>
                        `${REASON_LABELS[entry.reason] ?? entry.reason} ${entry.count}`,
                    )
                    .join(" · ")}
                </p>
              )}
              {feedback.pendingExport > 0 && (
                <p className="text-[11px] text-[#918d83]">
                  跑 <code>python -m eval.from_feedback</code>{" "}
                  把这些差评变成离线回归用例
                </p>
              )}
              <p className="text-[11px] text-[#918d83]">
                分母只算「被评价过的回答」，不是全部回答——没人点的那些既不算好也不算坏。
              </p>
            </div>
          )}

          <div className="space-y-2">
            <h4 className="text-[11px] font-semibold text-[#6e6b63] dark:text-[#a19f96]">
              最近的回答
            </h4>
            {traces.length === 0 && (
              <p className="text-[11px] text-[#918d83]">还没有埋点数据</p>
            )}
            {traces.map((item) => (
              <div key={item.traceId}>
                <button
                  onClick={() => openTrace(item.traceId)}
                  className="w-full flex items-center gap-3 px-3 py-2 rounded-xl bg-[#faf9f5] dark:bg-[#191817] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[11px] text-left"
                >
                  <ChevronRight
                    className={`w-3 h-3 shrink-0 transition-transform ${
                      trace?.traceId === item.traceId ? "rotate-90" : ""
                    }`}
                  />
                  <span className="flex-1 truncate text-[#6e6b63] dark:text-[#a19f96]">
                    {item.startedAt
                      ? new Date(item.startedAt).toLocaleString()
                      : item.traceId.slice(0, 8)}
                  </span>
                  <span className="flex items-center gap-1 text-[#1f1e1d] dark:text-[#edece8]">
                    <Timer className="w-3 h-3" /> {fmtMs(item.durationMs)}
                  </span>
                  <span className="text-[#6e6b63] dark:text-[#a19f96]">
                    {fmtInt(item.promptTokens + item.completionTokens)} tok
                  </span>
                  <span className="text-[#6e6b63] dark:text-[#a19f96]">
                    {fmtCost(item.cost, item.currency)}
                  </span>
                  {item.failures > 0 && (
                    <span className="text-red-500">{item.failures} 失败</span>
                  )}
                </button>
                {trace?.traceId === item.traceId && (
                  <div className="mt-1 ml-6 space-y-0.5">
                    {trace.roots.map((node) => (
                      <SpanRow key={node.id} node={node} depth={0} />
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
};

const Metric: React.FC<{ label: string; value: string; warn?: boolean }> = ({
  label,
  value,
  warn,
}) => (
  <div className="rounded-xl bg-[#faf9f5] dark:bg-[#191817] px-3 py-2.5">
    <div className="text-[10px] text-[#918d83] mb-0.5">{label}</div>
    <div
      className={`text-sm font-semibold ${
        warn ? "text-red-500" : "text-[#1f1e1d] dark:text-[#edece8]"
      }`}
    >
      {value}
    </div>
  </div>
);

const GroupTable: React.FC<{
  title: string;
  rows: UsageGroup[];
  labelOf: (row: UsageGroup) => string;
}> = ({ title, rows, labelOf }) => {
  if (!rows.length) return null;
  return (
    <div className="space-y-1.5">
      <h4 className="text-[11px] font-semibold text-[#6e6b63] dark:text-[#a19f96]">
        {title}
      </h4>
      <div className="space-y-0.5">
        {rows.map((row, index) => (
          <div
            key={`${labelOf(row)}-${row.currency ?? ""}-${index}`}
            className="flex items-center gap-3 px-3 py-1.5 rounded-lg bg-[#faf9f5] dark:bg-[#191817] text-[11px]"
          >
            <span className="flex-1 truncate text-[#1f1e1d] dark:text-[#edece8]">
              {labelOf(row)}
            </span>
            <span className="text-[#6e6b63] dark:text-[#a19f96]">{row.calls} 次</span>
            <span className="text-[#6e6b63] dark:text-[#a19f96]">
              {fmtInt(row.promptTokens + row.completionTokens)} tok
            </span>
            <span className="text-[#6e6b63] dark:text-[#a19f96]">
              均 {fmtMs(row.avgMs)}
            </span>
            <span className="w-20 text-right text-[#6e6b63] dark:text-[#a19f96]">
              {fmtCost(row.cost, row.currency)}
            </span>
            {row.failures > 0 && (
              <span className="text-red-500">{row.failures} 失败</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
};

const SpanRow: React.FC<{ node: TraceSpanNode; depth: number }> = ({
  node,
  depth,
}) => (
  <>
    <div
      className="flex items-center gap-2 text-[11px] py-0.5"
      style={{ paddingLeft: depth * 14 }}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full shrink-0 ${
          node.status === "ok"
            ? "bg-[#da7756]"
            : node.status === "cancelled"
              ? "bg-[#918d83]"
              : "bg-red-500"
        }`}
      />
      <span className="text-[#1f1e1d] dark:text-[#edece8]">{spanLabel(node.name)}</span>
      <span className="text-[#918d83]">{fmtMs(node.durationMs)}</span>
      {node.promptTokens !== null && (
        <span className="text-[#918d83]">
          {fmtInt((node.promptTokens ?? 0) + (node.completionTokens ?? 0))} tok
          {node.tokenSource === "estimated" && "（估算）"}
        </span>
      )}
      {node.errorType && <span className="text-red-500">{node.errorType}</span>}
    </div>
    {node.children.map((child) => (
      <SpanRow key={child.id} node={child} depth={depth + 1} />
    ))}
  </>
);
