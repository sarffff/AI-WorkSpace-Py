import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiClient } from "@/shared/api/client";
import type { FsBrowseResponse, FsEntry } from "@/shared/types/api.types";
import { fmtBytes } from "@/shared/lib/format";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import {
  ChevronLeft,
  File as FileIcon,
  Folder,
  FolderOpen,
  RefreshCw,
} from "lucide-react";

/**
 * 工作区文件树。
 *
 * ## 为什么是"一层一层点"而不是一次拉整棵树
 *
 * 授权根可以是任意本机目录——用户完全可能选一个几万个文件的目录。递归拉全量在
 * 那种目录下会让界面卡住，而绝大多数时候用户只想看最上面一两层。后端
 * `/fs/browse` 也因此是单层的，跟 `list_directory` 给模型的行为一致。
 *
 * ## 它不是文件管理器
 *
 * 这里**只读**：没有重命名、没有拖拽、没有删除。写操作全部走 agent + 审批卡片，
 * 因为那条路上有确认令牌和 diff 预览两道东西。在树上加一个删除按钮等于开一条
 * 绕过审批的旁路——审批闸门是文件能力唯一的用户可见防线。
 *
 * 点一个文件只是把它的路径交给上层（`onPick`），让用户可以把"就这个文件"直接
 * 说给 agent，省掉手打路径。
 */
interface Props {
  /** 用户点了某个文件。上层通常把路径塞进输入框 */
  onPick?: (entry: FsEntry) => void;
  onClose?: () => void;
}

export const FileTree: React.FC<Props> = ({ onPick, onClose }) => {
  const toast = useToast();
  const [view, setView] = useState<FsBrowseResponse | null>(null);
  const [loading, setLoading] = useState(false);
  // 当前请求的目录。null 表示"授权根列表"这一层
  const [path, setPath] = useState<string | null>(null);

  // toast 只在出错时用到，但**不能进 load 的依赖**：useToast 每次渲染返回的是
  // 一个新对象，进了依赖就会让 load 每次都换身份，挂着它的 useEffect 于是每渲染
  // 一次就重新请求一次目录——一个无限拉取的循环。放进 ref 里，effect 的依赖就
  // 只剩真正影响"该请求哪个目录"的东西。
  const toastRef = useRef(toast);
  toastRef.current = toast;

  const load = useCallback((target: string | null) => {
    setLoading(true);
    apiClient
      .browseFs(target ?? undefined)
      .then((body) => {
        setView(body);
        setPath(target);
      })
      .catch((e) => {
        // 失败时刻意**不动 view**：把上一层的内容留在屏幕上、同时弹错误，比清空
        // 好——清空之后用户面对一个空面板，不知道是目录空还是请求挂了。
        toastRef.current.error(toastMessageFrom(e, "读取目录失败"));
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load(null);
  }, [load]);

  const entries = view?.entries ?? [];
  // 判据取**返回值**里的 path，不是我们请求时用的那个：返回值才说明现在屏幕上
  // 是哪一层。用请求参数的话，一次失败的跳转之后两者就不一致了——屏幕上还是
  // 上一层的内容，而 path 已经指向那个没打开成功的目录。
  const atRootList = view ? view.path === null : path === null;

  return (
    <div className="w-64 border-r border-[#e6e2d8] dark:border-[#282724] bg-[#f3f0e6]/40 dark:bg-[#1a1917]/40 flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-3 border-b border-[#e6e2d8] dark:border-[#282724]">
        <div className="min-w-0">
          <div className="label-eyebrow">工作区</div>
          <span
            className="text-xs font-semibold text-[#1f1e1d] dark:text-[#edece8] truncate block"
            title={view?.path ?? undefined}
          >
            {view?.label ?? "已授权文件夹"}
          </span>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={() => load(path)}
            title="刷新"
            className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "anim-spin" : ""}`} />
          </button>
          {onClose && (
            <button
              onClick={onClose}
              title="收起"
              className="p-1 rounded hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[#918d83] hover:text-[#1f1e1d] dark:hover:text-[#edece8]"
            >
              <ChevronLeft className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* 返回上级只在后端给了 parent 时出现。根目录的上一级在沙箱外，
          后端给 null——显示了点下去必然 400 */}
      {view?.parent && (
        <button
          onClick={() => load(view.parent)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] border-b border-[#e6e2d8] dark:border-[#282724]"
        >
          <ChevronLeft className="w-3 h-3" />
          返回上级
        </button>
      )}
      {!view?.parent && !atRootList && (
        <button
          onClick={() => load(null)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] border-b border-[#e6e2d8] dark:border-[#282724]"
        >
          <ChevronLeft className="w-3 h-3" />
          全部文件夹
        </button>
      )}

      <div className="flex-1 overflow-y-auto p-1.5">
        {loading && entries.length === 0 && (
          <div className="px-2 py-2 text-[11px] text-[#918d83]">加载中...</div>
        )}

        {/* 空目录和"没授权任何文件夹"是两件事，给的下一步动作也不同 */}
        {!loading && entries.length === 0 && atRootList && (
          <div className="px-2 py-3 text-[11px] text-[#918d83] leading-relaxed">
            还没有授权文件夹。到设置里选一个，agent 才能读到本机文件。
          </div>
        )}
        {!loading && entries.length === 0 && !atRootList && (
          <div className="px-2 py-3 text-[11px] text-[#918d83]">空目录</div>
        )}

        {entries.map((entry) => (
          <button
            key={entry.path}
            onClick={() => (entry.isDir ? load(entry.path) : onPick?.(entry))}
            title={entry.path}
            className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-[#f3f0e6] dark:hover:bg-[#262522] text-[11px] text-left transition-colors group"
          >
            {entry.isRoot ? (
              <FolderOpen className="w-3.5 h-3.5 text-[#da7756] shrink-0" />
            ) : entry.isDir ? (
              <Folder className="w-3.5 h-3.5 text-[#918d83] shrink-0" />
            ) : (
              <FileIcon className="w-3.5 h-3.5 text-[#918d83] shrink-0" />
            )}
            <span className="flex-1 truncate text-[#1f1e1d] dark:text-[#edece8]">
              {entry.name}
            </span>
            <span className="text-[10px] text-[#918d83] shrink-0">
              {fmtBytes(entry.size)}
            </span>
          </button>
        ))}

        {view?.truncated && (
          <div className="px-2 py-2 text-[10px] text-amber-600 dark:text-amber-400 leading-relaxed">
            条目太多，只显示了前 {entries.length} 项。要找具体文件让 agent
            用搜索更快。
          </div>
        )}
      </div>
    </div>
  );
};
