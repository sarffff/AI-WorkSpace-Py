# -*- coding: utf-8 -*-
"""启动前判断：库里的 schema 版本落后于代码里的迁移链了吗。

## 为什么单独判断这一件事

``database.init_db()`` 一旦看到 ``alembic_version`` 表就不再建表——schema 从此归迁移管。
于是"拉了带新表的代码但没 ``upgrade head``"这个状态**可以安静地启动成功**：旧接口全
200，只有碰新表的路径报 1146 table doesn't exist，表现成一堆零散的 500 和一个后台
循环反复刷的 traceback，而不是一次明确的启动失败。这个脚本把那一次明确的失败还回来。

## 输出

一行 JSON（UTF-8），状态取其一：

- ``up-to-date``：current == head
- ``behind``：pending 列出缺的迁移，调用方据此决定要不要 upgrade
- ``unmanaged``：没有 ``alembic_version``（或表在但没有行）→ 首次启动，交给 init_db 建表
- ``branch``：迁移链有多个 head，alembic 无法确定顺序，得先合并分支
- ``unknown-revision``：库里的版本号在当前代码的迁移链里找不到（代码回滚过，或手工 stamp 过）
- ``unreachable``：连不上数据库

退出码：**拿到结论就返回 0**（结论在 JSON 里）；只有脚本自身跑不起来才非 0。这样调用方
能把"检查不可用"和"库需要迁移"分开，两者都不该 block 启动。

连接串里的密码不会出现在输出里——异常信息先过一遍脱敏。
"""
from __future__ import annotations

import json
import os
import re
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _emit(payload: dict) -> None:
    # 迁移的 docstring 是中文，而 Windows 管道默认按 GBK 编码，print 会炸或写出乱码
    text = json.dumps(payload, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(text.encode("utf-8"))


def _redact(message: str) -> str:
    return re.sub(r"://[^@/\s]*@", "://***@", message)


def main() -> int:
    payload = {
        "status": "error",
        "current": None,
        "head": None,
        "heads": [],
        "pending": [],
        "detail": None,
    }

    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
    except ImportError as exc:
        payload["detail"] = f"未安装 alembic: {type(exc).__name__}: {exc}"
        _emit(payload)
        return 1

    try:
        from sqlalchemy import inspect

        from database import engine
    except Exception as exc:  # config/.env 缺失、依赖装坏了，都从这里出去
        payload["detail"] = _redact(f"{type(exc).__name__}: {exc}")
        _emit(payload)
        return 1

    try:
        config = Config(os.path.join(_BACKEND_DIR, "alembic.ini"))
        config.set_main_option(
            "script_location", os.path.join(_BACKEND_DIR, "migrations")
        )
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
        payload["heads"] = heads
        payload["head"] = heads[0] if len(heads) == 1 else None

        if len(heads) > 1:
            payload["status"] = "branch"
            _emit(payload)
            return 0

        managed = "alembic_version" in inspect(engine).get_table_names()
        if not managed:
            payload["status"] = "unmanaged"
            _emit(payload)
            return 0

        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        payload["current"] = current

        if current is None:
            payload["status"] = "unmanaged"
            _emit(payload)
            return 0

        # walk_revisions 从 head 往 base 走，反转成 base -> head 的线性顺序
        chain = list(reversed(list(script.walk_revisions())))
        revisions = [revision.revision for revision in chain]

        if current not in revisions:
            payload["status"] = "unknown-revision"
            _emit(payload)
            return 0

        payload["pending"] = [
            {
                "revision": revision.revision,
                "message": (revision.doc or "").strip(),
            }
            for revision in chain[revisions.index(current) + 1 :]
        ]
        payload["status"] = "behind" if payload["pending"] else "up-to-date"
        _emit(payload)
        return 0
    except Exception as exc:
        # 连不上库和"库里的东西看不懂"都归到这里，由 detail 区分
        payload["status"] = "unreachable"
        payload["detail"] = _redact(f"{type(exc).__name__}: {exc}")
        _emit(payload)
        return 0


if __name__ == "__main__":
    sys.exit(main())
