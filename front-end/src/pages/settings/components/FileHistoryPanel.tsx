import React, { useCallback, useEffect, useState } from "react";
import { apiClient } from "@/shared/api/client";
import type { FsBackupsResponse } from "@/shared/types/api.types";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import { History, RefreshCw, RotateCcw } from "lucide-react";

/**
 * 写操作的旧版本，以及把它放回去。
 *
 * `write_file` 是整个覆盖，旧内容当场消失。审批闸门挡的是"模型偷偷写"，它挡不住
 * **用户点了同意之后后悔**——审批卡片上只看得到 diff 的前 60 行，同意之后才发现
 * 覆盖掉的是别的东西。这个面板就是为后一种情况存在的。
 *
 * ## 为什么这是界面而不是模型的工具
 *
 * 要撤销的是用户自己批准过的那次写，只有人能判断该不该撤。做成工具的话模型可以
 * 撤销自己的写，那是另一回事，而且会凭空多一个工具稀释工具面。
 *
 * ## 恢复可以来回走
 *
 * 恢复本身也是一次覆盖，后端在放回之前会先把当前内容再备份一份（action=restore）。
 * 所以"点错了恢复"不是又一次不可逆操作——那一条会出现在列表里，再点一次就回去了。
 *
 * ## 两种"列表为空"
 *
 * `enabled=false` 是后端没开写工具，这时列表必然空，说"还没有可恢复的版本"会让人
 * 以为写过但没留。`enabled=true` 且为空才是真的没写过。
 */
const ACTION_LABELS: Record<string, string> = {
  write: "覆盖写",
  edit: "改内容",
  delete: "删除",
  restore: "恢复前留存",
};

const fmtSize = (bytes: number): string =>
  bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`;

const fmtTime = (iso: string): string => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString([], {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
};

/** 只显示文件名 + 上一层目录：完整绝对路径又长又没信息量，悬停能看到全的 */
const shortPath = (path: string): string => {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/") || path;
};

export const FileHistoryPanel: React.FC<{ className?: string }> = ({
  className = "",
}) => {
  const toast = useToast();
  const [state, setState] = useState<FsBackupsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [restoringIds, setRestoringIds] = useState<Set<string>>(new Set());

  const refresh = useCallback(() => {
    apiClient
      .getFsBackups()
      .then((next) => {
        setState(next);
        setError(null);
      })
      .catch((e) =>
        setError(e instanceof Error ? e.message : "加载历史版本失败"),
      );
  }, []);

  useEffect(refresh, [refresh]);

  const restore = useCallback(
    async (id: string) => {
      setRestoringIds((prev) => new Set(prev).add(id));
      try {
        const { path } = await apiClient.restoreFsBackup(id);
        toast.success(`已恢复 ${shortPath(path)}`);
        refresh();
      } catch (e) {
        toast.error(toastMessageFrom(e, "恢复失败"));
      } finally {
        setRestoringIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [refresh, toast],
  );

  const backups = state?.backups ?? [];

  return (
    <section
      className={`rounded-2xl border border-[#e6e2d8] dark:border-[#282724] bg-[#faf9f5] dark:bg-[#191817] p-5 ${className}`}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <History className="w-4 h-4 text-[#6e6b63] dark:text-[#a19f96]" />
          <h3 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
            文件历史版本
          </h3>
        </div>
        <button
          onClick={refresh}
          className="p-1.5 rounded-lg hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[#918d83]"
          title="刷新"
        >
          <RefreshCw className="w-3.5 h-3.5" />
        </button>
      </div>

      <p className="text-[11px] text-[#918d83] mb-4 leading-relaxed">
        AI 覆盖写、改动或删除本机文件之前，会先留一份旧内容。
        恢复本身也会先留存当前版本，所以点错了还能再撤回来。
      </p>

      {error && (
        <div className="text-[11px] text-rose-500 mb-3">{error}</div>
      )}

      {state && !state.enabled && (
        <div className="text-[11px] text-[#918d83]">
          后端没有开启文件写入功能，因此不会产生历史版本。
        </div>
      )}

      {state?.enabled && backups.length === 0 && (
        <div className="text-[11px] text-[#918d83]">
          还没有任何写操作，所以没有可恢复的版本。
        </div>
      )}

      <div className="space-y-1.5">
        {backups.map((item) => {
          const busy = restoringIds.has(item.id);
          return (
            <div
              key={item.id}
              className="flex items-center gap-3 px-3 py-2 rounded-xl bg-[#f3f0e6]/60 dark:bg-[#201f1c]/60 text-[11px]"
            >
              <span
                className="flex-1 truncate text-[#1f1e1d] dark:text-[#edece8]"
                title={item.path}
              >
                {shortPath(item.path)}
              </span>
              <span className="text-[#918d83] shrink-0">
                {ACTION_LABELS[item.action] ?? item.action}
              </span>
              {/* 文件已经不在了要显眼：那是删除留下的备份，恢复等于把文件建回来 */}
              {!item.exists && (
                <span className="text-amber-600 dark:text-amber-400 shrink-0">
                  文件已不存在
                </span>
              )}
              <span className="text-[#918d83] shrink-0">
                {fmtSize(item.size)}
              </span>
              <span className="text-[#918d83] shrink-0">
                {fmtTime(item.createdAt)}
              </span>
              <button
                onClick={() => restore(item.id)}
                disabled={busy}
                className="flex items-center gap-1 px-2 py-1 rounded-lg text-[#da7756] hover:bg-[#da7756]/10 disabled:opacity-50 shrink-0"
              >
                <RotateCcw className="w-3 h-3" />
                {busy ? "恢复中" : "恢复"}
              </button>
            </div>
          );
        })}
      </div>
    </section>
  );
};
