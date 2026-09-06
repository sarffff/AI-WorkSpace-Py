import React, { useCallback, useEffect, useState } from "react";
import { apiClient } from "@/shared/api/client";
import { electronAPI } from "@/shared/api/electron";
import type { FsRootsResponse } from "@/shared/types/api.types";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import { FolderOpen, FolderPlus, RefreshCw, Trash2 } from "lucide-react";

/**
 * 本机文件夹授权。
 *
 * 授权过的文件夹里，AI 可以列目录、读文件、按内容搜索，以及（后端开了对应开关时）
 * 写、改、删。**沙箱只认这张表**：没授权的目录一律访问不了，路径里带 `..` 或者
 * 符号链接指向外面都会被拒（后端 services/fs_roots.py）。
 *
 * 撤销是即时生效的：下一轮对话里那个目录就不在范围内了，不需要重启。
 *
 * ## 为什么"没有文件能力"要分两种说法
 *
 * `enabled=false` 是后端没开这个功能——这时选文件夹也没用，得说清楚，否则用户
 * 点完发现什么都没变。`enabled=true` 且列表为空才是"该去授权一个"。
 *
 * ## 浏览器里没有系统对话框
 *
 * `electronAPI` 只在桌面端存在。浏览器里退回手打路径——那不是降级体验的问题，
 * 是浏览器根本拿不到本机路径。手打的授权效力和对话框选的完全相同（判据是这个
 * 请求带着谁的 JWT，不是路径怎么来的）。
 */
export const WorkspaceFolderPanel: React.FC<{ className?: string }> = ({
  className = "",
}) => {
  const toast = useToast();
  const [state, setState] = useState<FsRootsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [manualPath, setManualPath] = useState("");
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());

  const refresh = useCallback(() => {
    apiClient
      .getFsRoots()
      .then((next) => {
        setState(next);
        setError(null);
      })
      .catch((e) =>
        setError(e instanceof Error ? e.message : "加载文件夹授权失败"),
      );
  }, []);

  useEffect(refresh, [refresh]);

  const authorize = useCallback(
    async (path: string) => {
      if (!path.trim()) return;
      setBusy(true);
      try {
        await apiClient.addFsRoot(path.trim());
        setManualPath("");
        refresh();
        toast.success("已授权该文件夹");
      } catch (e) {
        // 后端的 400 detail 是可读的（"D:\nope 不是一个存在的目录"），
        // 比一句通用失败有用得多
        toast.error(toastMessageFrom(e, "授权失败"));
      } finally {
        setBusy(false);
      }
    },
    [refresh, toast],
  );

  const pick = useCallback(async () => {
    if (!electronAPI?.pickFolder) return;
    const picked = await electronAPI.pickFolder();
    // 取消时返回 null，不是错误，什么都不做
    if (picked) await authorize(picked);
  }, [authorize, electronAPI]);

  const revoke = useCallback(
    async (id: string) => {
      setDeletingIds((prev) => new Set(prev).add(id));
      try {
        await apiClient.removeFsRoot(id);
        refresh();
      } catch (e) {
        toast.error(toastMessageFrom(e, "撤销失败"));
      } finally {
        setDeletingIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [refresh, toast],
  );

  const roots = state?.roots ?? [];
  const canPick = Boolean(electronAPI?.pickFolder);

  return (
    <div
      className={`card-surface p-6 rounded-2xl space-y-4 relative z-10 ${className}`}
    >
      <div className="flex items-center justify-between pb-3 border-b border-[#e6e2d8] dark:border-[#282724]">
        <div className="flex items-center gap-2.5">
          <span className="w-6 h-6 rounded-lg bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
            <FolderOpen className="w-3.5 h-3.5" />
          </span>
          <h4 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
            本机文件夹
          </h4>
        </div>
        <button
          type="button"
          onClick={refresh}
          className="p-1.5 rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#e3dfd5]/60 dark:hover:bg-[#2e2d2a]"
          title="刷新"
        >
          <RefreshCw className="w-3.5 h-3.5" />
        </button>
      </div>

      <p className="text-xs leading-relaxed text-[#6e6b63] dark:text-[#a19f96]">
        授权过的文件夹里，AI 可以列目录、读文件、按内容搜索
        {state?.writeEnabled ? "，以及写入和修改文件" : ""}
        {state?.deleteEnabled ? "、删除文件" : ""}
        。没授权的目录一律访问不了。撤销后下一轮对话立即生效。
      </p>

      {/*
        后端没开这个功能时说清楚。少了这一段，用户会去选一个文件夹、
        看到它出现在列表里、然后发现 AI 说自己没有文件工具。
      */}
      {state && !state.enabled ? (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          服务端没有启用文件工具（<code>TOOL_FS_ENABLED</code>）。
          这里的授权会被保存，但要等服务端打开开关之后才生效。
        </div>
      ) : null}

      {error ? (
        <div className="text-xs text-rose-600 dark:text-rose-400">{error}</div>
      ) : null}

      {roots.length ? (
        <div className="rounded-lg border border-[#e3dfd5] dark:border-[#2e2d2a] divide-y divide-[#e3dfd5] dark:divide-[#2e2d2a]">
          {roots.map((root) => (
            <div
              key={root.id}
              className={`flex items-center gap-3 px-3 py-2.5 ${
                deletingIds.has(root.id) ? "opacity-50" : ""
              }`}
            >
              <FolderOpen className="w-3.5 h-3.5 shrink-0 text-[#a19f96]" />
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium text-[#3d3929] dark:text-[#e8e6dc]">
                  {root.label}
                </div>
                {/* 完整路径要显示出来：用户得能确认授权的是哪一个同名目录 */}
                <div className="text-[11px] break-all text-[#a19f96]">
                  {root.path}
                </div>
              </div>
              <button
                type="button"
                disabled={deletingIds.has(root.id)}
                onClick={() => revoke(root.id)}
                className="p-1.5 rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:bg-rose-500/10 hover:text-rose-600 disabled:opacity-50"
                title="撤销授权"
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-xs text-[#a19f96]">
          还没有授权任何文件夹，AI 现在读不到本机文件。
        </div>
      )}

      {canPick ? (
        <button
          type="button"
          disabled={busy}
          onClick={pick}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-[#da7756] hover:bg-[#c56646] text-white disabled:opacity-50"
        >
          <FolderPlus className="w-3.5 h-3.5" />
          {busy ? "授权中..." : "选择文件夹"}
        </button>
      ) : (
        /*
          浏览器里没有系统对话框——不是降级，是浏览器拿不到本机路径。
          手打的授权效力和对话框选的完全相同。
        */
        <div className="space-y-1.5">
          <div className="flex gap-2">
            <input
              value={manualPath}
              onChange={(e) => setManualPath(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") authorize(manualPath);
              }}
              placeholder="D:\\work\\资料"
              disabled={busy}
              className="flex-1 rounded-lg px-2.5 py-1.5 text-xs bg-[#faf9f5] dark:bg-[#1f1e1c] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#3d3929] dark:text-[#e8e6dc] placeholder:text-[#a19f96] focus:outline-none focus:border-[#da7756]/60"
            />
            <button
              type="button"
              disabled={busy || !manualPath.trim()}
              onClick={() => authorize(manualPath)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg bg-[#da7756] hover:bg-[#c56646] text-white disabled:opacity-50"
            >
              <FolderPlus className="w-3.5 h-3.5" />
              授权
            </button>
          </div>
          <div className="text-[11px] text-[#a19f96]">
            浏览器里无法调用系统文件夹选择框，请填写完整路径。桌面端会直接弹出对话框。
          </div>
        </div>
      )}
    </div>
  );
};
