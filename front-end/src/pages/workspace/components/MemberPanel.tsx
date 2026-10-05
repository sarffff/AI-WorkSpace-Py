import React, { useCallback, useEffect, useState } from "react";
import { apiClient } from "@/shared/api/client";
import type { WorkspaceInfo } from "@/shared/types/api.types";
import { toastMessageFrom, useToast } from "@/shared/ui/Toast";
import { RefreshCw, ShieldCheck, UserMinus, Users } from "lucide-react";

/**
 * 工作区成员：谁在里面、谁是管理员、把谁移出去。
 *
 * ## 为什么需要它
 *
 * 此前邀请码是个**单向阀**：进得来出不去。重置邀请码只防新人，员工离职之后他的
 * 账号仍然在工作区里、仍然能检索全部共享文档，而产品内没有任何办法处理——只能改库。
 *
 * ## 最后一个管理员那两个按钮
 *
 * 没有管理员的工作区是**不可恢复**的：改不了名、重置不了邀请码、管不了共享文档、
 * 再没人能把谁提成管理员（这个产品里没有超级管理员）。所以 `adminCount === 1` 时
 * 那位管理员的"降级"要禁掉。
 *
 * 禁用状态用后端给的 `adminCount`，不自己数 `members` 里的 admin：那是把一条
 * 不变量抄到第二个地方，而它在后端是拒绝的依据。两处漂移的表现是按钮可点、点了报错。
 *
 * ## 移除自己不在这里
 *
 * 自己那一行的移除按钮永久禁用。"退出工作区"是另一个动作（要先决定自己去哪儿），
 * 而用移除成员的接口把自己踢掉会让下一次请求静默补建一个个人空间。
 *
 * ## 移除要二次确认，改角色不要
 *
 * 移除会让那个人立刻失去对全部共享文档的访问，而且这个界面上没有"撤销"——
 * 要把他加回来得重新发邀请码。改角色是可逆的（点回去就好），多一次确认只是噪音。
 */
const ROLE_LABEL: Record<string, string> = {
  admin: "管理员",
  user: "成员",
  // 存量账号上的历史值，语义等同 user
  member: "成员",
};

export const MemberPanel: React.FC<{ className?: string }> = ({
  className = "",
}) => {
  const toast = useToast();
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  const refresh = useCallback(() => {
    apiClient
      .getWorkspace()
      .then((next) => {
        setWorkspace(next);
        setError(null);
      })
      .catch((e) =>
        setError(e instanceof Error ? e.message : "加载成员列表失败"),
      );
  }, []);

  useEffect(refresh, [refresh]);

  const withBusy = useCallback(
    async (id: string, action: () => Promise<WorkspaceInfo>) => {
      setBusyIds((prev) => new Set(prev).add(id));
      try {
        setWorkspace(await action());
        setError(null);
      } catch (e) {
        // 后端四种失败都回 400 + 中文 detail，直接透出去
        toast.error(toastMessageFrom(e, "操作失败"));
      } finally {
        setBusyIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }
    },
    [toast],
  );

  const changeRole = useCallback(
    (id: string, role: "admin" | "user") =>
      withBusy(id, async () => {
        const result = await apiClient.setMemberRole(id, role);
        toast.success(
          `${result.member?.name ?? "成员"} 现在是${ROLE_LABEL[role]}`,
        );
        return result.workspace;
      }),
    [toast, withBusy],
  );

  const remove = useCallback(
    (id: string) =>
      withBusy(id, async () => {
        const result = await apiClient.removeMember(id);
        toast.success(`已把 ${result.removed?.name ?? "该成员"} 移出工作区`);
        setConfirmingId(null);
        return result.workspace;
      }),
    [toast, withBusy],
  );

  const isAdmin = workspace?.isAdmin ?? workspace?.role === "admin";
  const members = workspace?.members ?? [];
  // 缺字段时（旧后端）退回自己数，但那只用于禁用判断的兜底
  const adminCount =
    workspace?.adminCount ??
    members.filter((item) => item.role === "admin").length;

  return (
    <section
      className={`rounded-2xl border border-[#e6e2d8] dark:border-[#282724] bg-[#faf9f5] dark:bg-[#191817] p-5 ${className}`}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Users className="w-4 h-4 text-[#6e6b63] dark:text-[#a19f96]" />
          <h3 className="text-sm font-semibold text-[#1f1e1d] dark:text-[#edece8]">
            工作区成员
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

      {error && <div className="text-[11px] text-rose-500 mb-3">{error}</div>}

      {workspace && !isAdmin && (
        <p className="text-[11px] text-[#918d83] leading-relaxed">
          这个工作区共 {workspace.memberCount} 名成员。
          只有管理员能调整角色或移除成员。
        </p>
      )}

      {workspace && isAdmin && (
        <>
          <p className="text-[11px] text-[#918d83] mb-4 leading-relaxed">
            移除成员之后，他会立刻失去对本工作区共享文档的访问；
            他自己上传的私有文档跟着他走，不会留在这里，也不会被删。
          </p>

          <div className="space-y-1.5">
            {members.map((member) => {
              const busy = busyIds.has(member.id);
              const memberIsAdmin = member.role === "admin";
              // 最后一个管理员不能降级：降了就没人管得了这个空间
              const lastAdmin = memberIsAdmin && adminCount <= 1;
              const confirming = confirmingId === member.id;

              return (
                <div
                  key={member.id}
                  className="flex items-center gap-2 px-3 py-2 rounded-xl bg-white dark:bg-[#201f1c] border border-[#e6e2d8]/60 dark:border-[#282724]/60"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[12px] text-[#1f1e1d] dark:text-[#edece8] truncate">
                        {member.name}
                      </span>
                      {member.isSelf && (
                        <span className="text-[9px] px-1.5 py-0.5 rounded bg-[#f3f0e6] dark:bg-[#262522] text-[#918d83]">
                          你
                        </span>
                      )}
                      {memberIsAdmin && (
                        <ShieldCheck
                          className="w-3 h-3 text-[#da7756]"
                          aria-label="管理员"
                        />
                      )}
                    </div>
                    {member.email && (
                      <div
                        className="text-[10px] text-[#918d83] truncate"
                        title={member.email}
                      >
                        {member.email}
                      </div>
                    )}
                  </div>

                  {confirming ? (
                    <>
                      <span className="text-[10px] text-rose-500">
                        确定移出？
                      </span>
                      <button
                        onClick={() => remove(member.id)}
                        disabled={busy}
                        className="px-2 py-1 rounded-lg text-[10px] bg-rose-500 text-white disabled:opacity-50"
                      >
                        移出
                      </button>
                      <button
                        onClick={() => setConfirmingId(null)}
                        className="px-2 py-1 rounded-lg text-[10px] text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#f3f0e6] dark:hover:bg-[#262522]"
                      >
                        取消
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        onClick={() =>
                          changeRole(
                            member.id,
                            memberIsAdmin ? "user" : "admin",
                          )
                        }
                        disabled={busy || lastAdmin}
                        title={
                          lastAdmin
                            ? "这是最后一个管理员，降级之后就没人能管理这个工作区了"
                            : undefined
                        }
                        className="px-2 py-1 rounded-lg text-[10px] text-[#6e6b63] dark:text-[#a19f96] hover:bg-[#f3f0e6] dark:hover:bg-[#262522] disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        {memberIsAdmin ? "降为成员" : "设为管理员"}
                      </button>
                      <button
                        onClick={() => setConfirmingId(member.id)}
                        disabled={busy || member.isSelf}
                        title={
                          member.isSelf
                            ? "不能移除自己。要离开这个工作区请用邀请码加入别的空间"
                            : undefined
                        }
                        className="p-1.5 rounded-lg text-[#918d83] hover:bg-rose-500/10 hover:text-rose-500 disabled:opacity-40 disabled:cursor-not-allowed"
                        aria-label={`移除 ${member.name}`}
                      >
                        <UserMinus className="w-3.5 h-3.5" />
                      </button>
                    </>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </section>
  );
};
