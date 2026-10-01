"""审计链查询接口。

只做两件事：列出**当前用户自己**的审计条目，以及校验自己那条链的完整性。

## 为什么全部按 current_user 自作用域

全项目没有 admin/role 概念，所有读接口（见 metrics_router）都按 ``user_id`` 过滤。
审计链本身就是**按 actor 分段**的（见 services/audit_log.py），所以"只看自己那条"
既是访问控制，也正好是链的天然边界——一个用户 verify 的就是它自己那一段。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import User
from services import audit_log

router = APIRouter(prefix="/audit", tags=["审计"])


@router.get("")
async def list_audit(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """当前用户的审计条目，按 seq 倒序（新的在前）。"""
    return {
        "entries": audit_log.history(db, current_user.id, limit=limit, offset=offset),
    }


@router.get("/verify")
async def verify_audit(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """校验当前用户审计链的完整性。

    ``firstBrokenSeq`` 非空即那一条起链接对不上——它或它之前被改过/删过。
    """
    return audit_log.verify(db, current_user.id)
