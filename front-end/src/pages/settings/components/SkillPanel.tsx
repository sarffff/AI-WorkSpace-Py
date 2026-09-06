import React, { useCallback, useEffect, useState } from "react";
import { apiClient } from "@/shared/api/client";
import type { SkillsResponse, WorkspaceSkill } from "@/shared/types/api.types";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import {
  BookMarked,
  FileText,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";

/**
 * 作业指导（skill）管理。
 *
 * 一份 skill 回答"这件事在本组织该怎么做"——报销怎么审、季度报告怎么写。AI 处理
 * 任务前会看清单，命中就先加载再动手。
 *
 * 两层，能做的操作不同：
 *
 * - **内置**：仓库里带的，只读。要改得改代码、走 review。
 * - **工作区**：本工作区自己写的，只有管理员能改。写好即生效，不用重启。
 *
 * 同名时工作区那份盖掉内置——所以内置列表里要标出"已被覆盖"，否则管理员看不出
 * 自己写的那份到底有没有生效。
 *
 * 停用（而不是删除）会退回内置那份：改坏一条之后想先关掉看看，比删了重录便宜。
 */

const EMPTY_DRAFT = { name: "", description: "", instructions: "", enabled: true };

export const SkillPanel: React.FC<{ className?: string }> = ({
  className = "",
}) => {
  const toast = useToast();
  const [state, setState] = useState<SkillsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<typeof EMPTY_DRAFT | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    apiClient
      .getSkills()
      .then((next) => {
        setState(next);
        setError(null);
      })
      .catch((e) =>
        setError(e instanceof Error ? e.message : "加载作业指导失败"),
      );
  }, []);

  useEffect(refresh, [refresh]);

  const save = useCallback(async () => {
    if (!draft) return;
    setBusy(true);
    try {
      await apiClient.saveSkill(draft);
      setDraft(null);
      refresh();
      toast.success("已保存");
    } catch (e) {
      // 后端的 400 detail 是可读的（"name 只能包含字母、数字…"）
      toast.error(toastMessageFrom(e, "保存失败"));
    } finally {
      setBusy(false);
    }
  }, [draft, refresh, toast]);

  const remove = useCallback(
    async (skill: WorkspaceSkill) => {
      setBusy(true);
      try {
        await apiClient.deleteSkill(skill.id);
        refresh();
      } catch (e) {
        toast.error(toastMessageFrom(e, "删除失败"));
      } finally {
        setBusy(false);
      }
    },
    [refresh, toast],
  );

  const canEdit = state?.canEdit ?? false;

  return (
    <div
      className={`card-surface p-6 rounded-2xl space-y-4 relative z-10 ${className}`}
    >
      <div className="flex items-center justify-between pb-3 border-b border-[#e6e2d8] dark:border-[#282724]">
        <div className="flex items-center gap-2.5">
          <span className="w-6 h-6 rounded-lg bg-[#da7756]/12 text-[#da7756] flex items-center justify-center">
            <BookMarked className="w-3.5 h-3.5" />
          </span>
          <h4 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
            作业指导
          </h4>
        </div>
        <div className="flex items-center gap-1">
          {canEdit && !draft ? (
            <button
              type="button"
              onClick={() => setDraft({ ...EMPTY_DRAFT })}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs rounded-lg text-[#da7756] hover:bg-[#da7756]/10"
            >
              <Plus className="w-3.5 h-3.5" />
              新增
            </button>
          ) : null}
          <button
            type="button"
            onClick={refresh}
            className="p-1.5 rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#e3dfd5]/60 dark:hover:bg-[#2e2d2a]"
            title="刷新"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      <p className="text-xs leading-relaxed text-[#6e6b63] dark:text-[#a19f96]">
        AI 处理任务之前会先看这份清单，命中的话按对应的指导执行。清单里只有名字和
        用途，正文由它按需取用。
      </p>

      {/*
        后端没开这个功能时说清楚。少了这一段，管理员会写完一份 SOP、
        看到它出现在列表里、然后发现 AI 完全不按它办事。
      */}
      {state && !state.enabled ? (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          服务端没有启用作业指导（<code>SKILL_ENABLED</code>）。
          这里写的内容会保存，但要等服务端打开开关之后 AI 才看得到。
        </div>
      ) : null}

      {error ? (
        <div className="text-xs text-rose-600 dark:text-rose-400">{error}</div>
      ) : null}

      {/* ---- 编辑器 ---- */}
      {draft ? (
        <div className="rounded-lg border border-[#da7756]/40 bg-[#da7756]/[0.04] p-3 space-y-2.5">
          <div className="flex gap-2">
            <input
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              placeholder="名字（字母数字连字符，AI 用它引用）"
              className="flex-1 rounded-lg px-2.5 py-1.5 text-xs bg-white dark:bg-[#151412] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#3d3929] dark:text-[#e8e6dc] placeholder:text-[#a19f96] focus:outline-none focus:border-[#da7756]"
            />
          </div>
          <input
            value={draft.description}
            onChange={(e) =>
              setDraft({ ...draft, description: e.target.value })
            }
            placeholder="一句话说明什么时候该用它"
            className="w-full rounded-lg px-2.5 py-1.5 text-xs bg-white dark:bg-[#151412] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#3d3929] dark:text-[#e8e6dc] placeholder:text-[#a19f96] focus:outline-none focus:border-[#da7756]"
          />
          {/*
            这句提示不是客套：description 是 AI 选用这份指导的**唯一**依据
            （清单里只有它）。写不清楚的话这份指导永远不会被选中，而那不报错。
          */}
          <div className="text-[11px] text-[#a19f96]">
            这一句是 AI 判断"要不要用这份指导"的唯一依据，写清适用场景。
          </div>
          <textarea
            value={draft.instructions}
            onChange={(e) =>
              setDraft({ ...draft, instructions: e.target.value })
            }
            rows={8}
            placeholder={"具体怎么做。例如：\n1. 先确认出差城市与日期\n2. 从知识库查额度上限，不要凭印象\n3. 逐项核对并列出超出部分"}
            className="w-full resize-y rounded-lg px-2.5 py-2 text-xs font-mono bg-white dark:bg-[#151412] border border-[#e3dfd5] dark:border-[#2e2d2a] text-[#3d3929] dark:text-[#e8e6dc] placeholder:text-[#a19f96] focus:outline-none focus:border-[#da7756]"
          />
          <div className="flex items-center justify-between">
            <label className="flex items-center gap-1.5 text-xs text-[#6e6b63] dark:text-[#a19f96]">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(e) =>
                  setDraft({ ...draft, enabled: e.target.checked })
                }
              />
              启用
            </label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setDraft(null)}
                className="px-3 py-1.5 text-xs rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#e3dfd5]/60 dark:hover:bg-[#2e2d2a]"
              >
                取消
              </button>
              <button
                type="button"
                disabled={
                  busy ||
                  !draft.name.trim() ||
                  !draft.description.trim() ||
                  !draft.instructions.trim()
                }
                onClick={save}
                className="px-3 py-1.5 text-xs rounded-lg bg-[#da7756] hover:bg-[#c56646] text-white disabled:opacity-50"
              >
                {busy ? "保存中..." : "保存"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {/* ---- 工作区 skill ---- */}
      {state?.workspace.length ? (
        <div className="space-y-1.5">
          <div className="text-[11px] font-medium text-[#6e6b63] dark:text-[#a19f96]">
            本工作区（{state.workspace.length}）
          </div>
          <div className="rounded-lg border border-[#e3dfd5] dark:border-[#2e2d2a] divide-y divide-[#e3dfd5] dark:divide-[#2e2d2a]">
            {state.workspace.map((skill) => (
              <div
                key={skill.id}
                className={`px-3 py-2.5 ${skill.enabled ? "" : "opacity-55"}`}
              >
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="text-xs font-medium text-[#3d3929] dark:text-[#e8e6dc]">
                        {skill.name}
                      </span>
                      {!skill.enabled ? (
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-[#e3dfd5] dark:bg-[#2e2d2a] text-[#6e6b63] dark:text-[#a19f96]">
                          已停用
                        </span>
                      ) : null}
                    </div>
                    <div className="mt-0.5 text-[11px] text-[#6e6b63] dark:text-[#a19f96]">
                      {skill.description}
                    </div>
                  </div>
                  {canEdit ? (
                    <div className="flex gap-0.5 shrink-0">
                      <button
                        type="button"
                        onClick={() =>
                          setDraft({
                            name: skill.name,
                            description: skill.description,
                            instructions: skill.instructions,
                            enabled: skill.enabled,
                          })
                        }
                        className="p-1.5 rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#e3dfd5]/60 dark:hover:bg-[#2e2d2a]"
                        title="编辑"
                      >
                        <Pencil className="w-3.5 h-3.5" />
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => remove(skill)}
                        className="p-1.5 rounded-lg text-[#6e6b63] dark:text-[#a19f96] hover:bg-rose-500/10 hover:text-rose-600 disabled:opacity-50"
                        title="删除"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {/* ---- 内置 skill ---- */}
      {state?.builtin.length ? (
        <div className="space-y-1.5">
          <div className="text-[11px] font-medium text-[#6e6b63] dark:text-[#a19f96]">
            内置（{state.builtin.length}，只读）
          </div>
          <div className="rounded-lg border border-[#e3dfd5] dark:border-[#2e2d2a] divide-y divide-[#e3dfd5] dark:divide-[#2e2d2a]">
            {state.builtin.map((skill) => (
              <div
                key={skill.name}
                className={`px-3 py-2.5 ${skill.overridden ? "opacity-55" : ""}`}
              >
                <div className="flex items-center gap-1.5">
                  <span className="text-xs font-medium text-[#3d3929] dark:text-[#e8e6dc]">
                    {skill.name}
                  </span>
                  {/*
                    被同名工作区 skill 盖掉时必须标出来：不标的话管理员看不出
                    内置那份已经不生效，反过来也会以为自己写的那份没被采用。
                  */}
                  {skill.overridden ? (
                    <span className="px-1.5 py-0.5 rounded text-[10px] bg-amber-500/15 text-amber-700 dark:text-amber-400">
                      已被本工作区同名指导覆盖
                    </span>
                  ) : null}
                </div>
                <div className="mt-0.5 text-[11px] text-[#6e6b63] dark:text-[#a19f96]">
                  {skill.description}
                </div>
                {skill.attachments.length ? (
                  <div className="mt-1 flex flex-wrap items-center gap-1">
                    {skill.attachments.map((filename) => (
                      <span
                        key={filename}
                        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-[#e3dfd5]/70 dark:bg-[#2e2d2a] text-[#6e6b63] dark:text-[#a19f96]"
                      >
                        <FileText className="w-2.5 h-2.5" />
                        {filename}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {!state?.workspace.length && !state?.builtin.length && !error ? (
        <div className="text-xs text-[#a19f96]">
          还没有任何作业指导。
          {canEdit ? "点「新增」写一份，AI 之后处理相关任务时会按它执行。" : ""}
        </div>
      ) : null}

      {state && !canEdit ? (
        <div className="flex items-center gap-1.5 text-[11px] text-[#a19f96]">
          <X className="w-3 h-3" />
          只有工作区管理员可以修改作业指导
        </div>
      ) : null}
    </div>
  );
};
