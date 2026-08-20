import React, { useEffect, useReducer } from "react";
import { apiClient } from "@/shared/api/client";
import type { AgentMetrics } from "@/shared/types/api.types";
import { Bot, Loader2 } from "lucide-react";
import { Body } from "../components/Body";

type State = {
  metrics: AgentMetrics | null;
  loading: boolean;
  error: string | null;
};

type Action =
  | { type: "LOADING" }
  | { type: "SUCCESS"; payload: AgentMetrics }
  | { type: "ERROR"; payload: string };

const initialState: State = {
  metrics: null,
  loading: true,
  error: null,
};

const reducer = (state: State, action: Action): State => {
  switch (action.type) {
    case "LOADING":
      return { ...state, loading: true, error: null };
    case "SUCCESS":
      return { metrics: action.payload, loading: false, error: null };
    case "ERROR":
      return { metrics: null, loading: false, error: action.payload };
    default:
      return state;
  }
};

const RANGES = [1, 7, 30] as const;


/**
 * 委派 / 审批 / 子代理的线上指标面板。
 *
 * 存在的理由只有一个：**回答"委派到底值不值"**。多代理已经能跑，但"它比单代理好"
 * 到目前为止只是一个说法。委派的代价是确定的（每次一个完整的嵌套循环），收益是
 * 不确定的，而这个不确定性在任何单次回答里都看不出来——只有把一段时间的执行
 * 聚起来才看得见。
 *
 * 所以这个面板的重心不是"委派了多少次"（那只是个计数），而是最下面那张
 * 委派 vs 未委派的对比表：多花了几倍钱、慢了几倍。
 */
export const AgentMetricsPanel: React.FC = () => {
  const [days, setDays] = React.useState<number>(7);
  const [state, dispatch] = useReducer(reducer, initialState);

  useEffect(() => {
    const abortController = new AbortController();
    dispatch({ type: "LOADING" });

    apiClient
      .getAgentMetrics(days)
      .then((data) => {
        dispatch({ type: "SUCCESS", payload: data });
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        dispatch({
          type: "ERROR",
          payload: err instanceof Error ? err.message : "加载 Agent 指标失败",
        });
      });

    return () => {
      abortController.abort();
    };
  }, [days]);

  return (
    <div className="card-surface rounded-2xl p-5 space-y-5 relative z-10">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8] flex items-center gap-2">
          <span className="w-6 h-6 rounded-lg bg-violet-500/12 text-violet-500 flex items-center justify-center">
            <Bot className="w-3.5 h-3.5" />
          </span>
          Agent 执行
        </h3>
        <div className="flex gap-1">
          {RANGES.map((range) => (
            <button
              key={range}
              onClick={() => setDays(range)}
              className={`px-2.5 py-1 text-[11px] rounded-lg transition-colors ${
                days === range
                  ? "bg-[#da7756]/12 text-[#da7756] font-medium"
                  : "text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#faf9f5] dark:hover:bg-[#191817]"
              }`}
            >
              {range} 天
            </button>
          ))}
        </div>
      </div>

      {state.loading && (
        <div className="flex items-center gap-2 text-[11px] text-[#6e6b63] dark:text-[#a19f96] py-6 justify-center">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          加载中…
        </div>
      )}

      {state.error && !state.loading && (
        <div className="text-[11px] text-red-500 py-4 text-center">
          {state.error}
        </div>
      )}

      {state.metrics && !state.loading && !state.error && (
        <Body metrics={state.metrics} />
      )}
    </div>
  );
};

