from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_user
from config import settings as app_settings
from database import get_db
from models import User
from redis_service import redis_service
from services.settings_service import (
    available_models,
    is_model_allowed,
    load_preferences,
    save_preferences,
)

router = APIRouter(prefix="/settings", tags=["设置"])

class PreferencesUpdate(BaseModel):
    defaultModel: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    maxTokens: int | None = Field(default=None, ge=128, le=8192)
    topP: float | None = Field(default=None, gt=0, le=1)


@router.get("")
async def get_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取应用设置：服务端配置 + 用户偏好"""
    prefs = load_preferences(current_user.id)
    return {
        "server": {
            "llmBaseUrl": app_settings.LLM_BASE_URL,
            "configuredModel": app_settings.LLM_MODEL,
            "embeddingModel": app_settings.EMBEDDING_MODEL,
            "redisEnabled": bool(redis_service.enabled and redis_service.client),
            "databaseUrl": _mask_db_url(app_settings.DATABASE_URL),
        },
        "preferences": prefs,
        "availableModels": available_models(),
    }


@router.patch("/preferences")
async def update_preferences(
    body: PreferencesUpdate,
    current_user: User = Depends(get_current_user),
):
    """更新用户偏好（部分更新）"""
    current = load_preferences(current_user.id)
    updates = body.model_dump(exclude_none=True)
    requested_model = updates.get("defaultModel")
    if requested_model and not is_model_allowed(requested_model):
        raise HTTPException(status_code=400, detail="当前模型服务不支持该模型")
    current.update(updates)
    save_preferences(current_user.id, current)
    return {"success": True, "preferences": current}


def _mask_db_url(url: str) -> str:
    """隐藏数据库 URL 中的密码"""
    if "://" not in url:
        return url
    head, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _pw = creds.split(":", 1)
        return f"{head}://{user}:****@{host}"
    return f"{head}://{creds}@{host}"
