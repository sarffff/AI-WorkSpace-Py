"""本机文件夹授权的查看、添加与撤销。

文件系统工具只能在这里登记过的目录下工作（沙箱见 ``services/fs_roots.py``）。
一个用户名下没有任何一行时，那些工具**根本不注册**。

## 为什么添加是一个普通的 POST 而不是"服务端弹对话框"

目录选择必须发生在**用户那台机器**上。桌面端由 Electron 主进程调
``dialog.showOpenDialog``，用户在系统对话框里选完，渲染进程把路径 POST 到这里。
服务端只做校验与登记——它没有、也不该有弹出对话框的能力。

这也意味着这个接口收的是一个**任意字符串**。所以 ``add_root`` 要求它此刻真的是
一个目录：授权一个不存在的路径没有意义，而且会让"为什么模型说找不到文件"变成
一个查不出来的问题。

## 为什么不校验"这个路径是不是用户真的选过的"

做不到，也不必要。校验的对象是"这个用户愿不愿意让 agent 访问这个目录"，而这个
请求本身就带着他的 JWT——他自己手打一个路径和在对话框里点一个目录，授权效力
完全相同。要挡的是**别人**替他授权，那件事由 ``get_current_user`` 挡住。
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from config import settings
from database import get_db
from models import User
from services import fs_backup, fs_policy, fs_roots, fs_tools

router = APIRouter(prefix="/fs", tags=["本机文件夹"])


class AddRootRequest(BaseModel):
    # 512 与 workspace_roots.path 那一列对齐。超长直接 422，而不是入库时静默截断——
    # 截断的后果是沙箱根变成一个**不同的目录**，前缀校验照样通过
    path: str = Field(min_length=1, max_length=512)
    label: str | None = Field(default=None, max_length=120)


@router.get("/roots")
async def list_roots(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """已授权的文件夹，以及文件能力当前的开关状态。

    把开关状态一起返回，是因为前端要能区分两种"没有文件能力"：**没授权**（引导
    用户去选一个文件夹）和**后端没开这个功能**（选了也没用，该说清楚而不是让
    用户点完发现没变化）。
    """
    return {
        "roots": fs_roots.describe_roots(db, current_user.id),
        "enabled": fs_tools.enabled(),
        "writeEnabled": settings.TOOL_FS_WRITE_ENABLED,
        "deleteEnabled": settings.TOOL_FS_DELETE_ENABLED,
        "tools": fs_tools.enabled_names(),
    }


@router.get("/browse")
async def browse(
    path: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列一个已授权目录下的条目，给界面用。

    ## 为什么不复用 list_directory

    那个是**给模型看的**：返回一段中文散文（"xxx 下共 12 项"），文件名过
    ``mask_markup``，路径压成相对形式来省工具结果预算。界面要的是结构化的
    name/isDir/size，还要能拿到绝对路径去请求下一层。硬套会两边都别扭。

    ## 沙箱走同一个函数

    ``fs_roots.resolve_within_roots``——和六个工具完全同一个调用。这里**不允许**
    出现第二套路径校验：沙箱是文件能力唯一的边界，压着 20 条逃逸测试，复制一份
    出来意味着那些测试只保护其中一份。

    省略 path 时返回授权根列表本身，而不是报错：界面第一次打开时还不知道有什么。
    """
    roots = fs_roots.describe_roots(db, current_user.id)
    if path is None or not path.strip():
        return {
            "path": None,
            "label": None,
            "parent": None,
            "entries": [
                {
                    "name": root["label"],
                    "path": root["path"],
                    "isDir": True,
                    "size": None,
                    "isRoot": True,
                }
                for root in roots
            ],
            "truncated": False,
        }

    try:
        target = fs_roots.resolve_within_roots(db, current_user.id, path)
    except fs_roots.RootError as exc:
        # 400 而不是 403：越界和"路径拼错了"在这里是同一类客户端错误，而且
        # 区分开会把"这个目录存在但你没权限"漏给调用方
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail="不是一个目录")

    try:
        names = sorted(os.listdir(target))
    except OSError as exc:
        raise HTTPException(
            status_code=400, detail=exc.strerror or "读取目录失败"
        ) from exc

    limit = max(1, settings.FS_LIST_MAX_ENTRIES)
    dirs: list[dict] = []
    files: list[dict] = []
    for name in names:
        if name in fs_tools.SKIP_DIRS:
            continue
        full = os.path.join(target, name)
        is_dir = os.path.isdir(full)
        size = None
        if not is_dir:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = None
        # 受凭据保护的条目在界面上也要看得出来。这里**只标记不隐藏**：
        # 这是用户浏览自己的文件夹，藏起来等于骗他；而他需要知道的是
        # "助手看不到这个文件"——那正是他授权这个文件夹时会关心的事。
        protected = fs_policy.is_protected(
            os.path.join(full, "x") if is_dir else full
        )
        (dirs if is_dir else files).append(
            {
                "name": name,
                "path": full,
                "isDir": is_dir,
                "protected": protected,
                "size": size,
                "isRoot": False,
            }
        )

    entries = dirs + files
    # 父目录只在还落在授权根内时给出，否则界面会做出一个必然 400 的请求
    parent = os.path.dirname(target)
    if parent == target:
        parent = None
    else:
        try:
            fs_roots.resolve_within_roots(db, current_user.id, parent)
        except fs_roots.RootError:
            parent = None

    return {
        "path": target,
        "label": fs_tools.relative_label(target, roots),
        "parent": parent,
        "entries": entries[:limit],
        "truncated": len(entries) > limit,
    }


@router.get("/backups")
async def list_backups(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """写操作留下的旧版本，最新的在前。

    ## 为什么这是一个界面接口，而不是给模型的工具

    要撤销的是**用户自己批准过的那次写**——审批卡片上只看得到 diff 的前 60 行，
    同意之后才发现覆盖掉的是别的东西。这件事只有人能判断。做成工具的话模型可以
    撤销自己的写，那是另一回事，而且会凭空多一个工具稀释工具面。

    每一条都重新过一次沙箱（见 ``fs_backup.list_for_user``）：授权撤销之后旧备份
    不该还能恢复，那等于绕过"用户已经收回了这个目录的权限"。
    """
    return {
        "backups": fs_backup.list_for_user(db, current_user.id),
        "enabled": fs_tools.enabled() and settings.TOOL_FS_WRITE_ENABLED,
    }


@router.post("/backups/{backup_id}/restore")
async def restore_backup(
    backup_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把某份旧版本放回原位。

    恢复本身也是一次覆盖，所以 ``fs_backup.restore`` 会先把当前内容再备份一份——
    "点错了恢复"不该是又一次不可逆操作。
    """
    try:
        path = fs_backup.restore(db, current_user.id, backup_id)
    except fs_backup.BackupError as exc:
        # 400 而不是 404：不存在、不属于你、内容文件丢了，对调用方是同一类结果，
        # 而区分开会把"别人有哪些备份"漏出去
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "path": path}


@router.post("/roots")
async def add_root(
    payload: AddRootRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        root = fs_roots.add_root(
            db,
            current_user.id,
            payload.path,
            workspace_id=current_user.workspace_id,
            label=payload.label,
        )
    except fs_roots.RootError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": root.id,
        "path": root.path,
        "label": root.label,
    }


@router.delete("/roots/{root_id}")
async def remove_root(
    root_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not fs_roots.remove_root(db, current_user.id, root_id):
        raise HTTPException(status_code=404, detail="该授权不存在")
    return {"ok": True}
