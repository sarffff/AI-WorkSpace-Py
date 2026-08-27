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
from services.web_search import web_search_client
from services import agent_roles, approval, file_types, subagent

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
        # 前端需要据此改变行为，不只是显示一个开关状态：
        # ``readAttachment`` 决定文本附件是"只给路径"还是"内联全文"——工具没开
        # 却只给路径，等于把附件内容彻底丢掉，模型拿到一个它读不了的字符串。
        # ``toolHistory`` 决定工具轨迹面板在历史回合里为空时该怎么解释。
        # web_search 报的是"真的注册了吗"，而不是开关值：开关开了但没配 key 时
        # 那个工具根本不注册，只报开关值会让界面说谎。
        "capabilities": {
            "calculate": app_settings.TOOL_CALCULATE_ENABLED,
            "readAttachment": app_settings.TOOL_READ_ATTACHMENT_ENABLED,
            "webSearch": (
                app_settings.TOOL_WEB_SEARCH_ENABLED and web_search_client.configured
            ),
            "writeKnowledge": app_settings.TOOL_WRITE_KNOWLEDGE_ENABLED,
            "toolHistory": app_settings.TOOL_HISTORY_ENABLED,
            # 委派模式与可用角色。前端据此决定工具轨迹里要不要按角色分组,
            # 以及在 delegate 那一步下面留出缩进的位置。
            "delegation": {
                "mode": app_settings.AGENT_DELEGATION_MODE
                if subagent.enabled()
                else "off",
                "roles": agent_roles.names() if subagent.enabled() else [],
                "maxDelegations": app_settings.AGENT_MAX_DELEGATIONS,
            },
            # 审批与快照。前端据此决定要不要渲染审批卡片、要不要去查待审批列表——
            # 关着的时候那两条路径完全不该出现,而不是渲染出来点了没反应。
            "approval": {
                "mode": app_settings.AGENT_APPROVAL_MODE
                if approval.enabled()
                else "off",
                "tools": sorted(approval.gated_tools()),
                "checkpoints": app_settings.AGENT_CHECKPOINT_ENABLED,
            },
            # 能上传哪些文件。放在 capabilities 里而不是 server 里：它和上面几项
            # 一样是"前端据此改变行为"，不是拿来显示的配置值——文件选择器的 accept、
            # 以及"这个扩展名走内联还是走知识库"的分派都由它决定。
            # 前端不再自己维护扩展名清单，理由见 services/file_types.py。
            "fileTypes": file_types.payload(),
        },
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
