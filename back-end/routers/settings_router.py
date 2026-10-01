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
from services import (
    agent_roles,
    approval,
    file_types,
    fs_roots,
    fs_tools,
    prompt_library,
    subagent,
)

router = APIRouter(prefix="/settings", tags=["设置"])


def _split_csv(value: str | None) -> list[str]:
    """把逗号分隔的配置拆成去空的列表。VISION_MODELS / LLM_FALLBACK_MODELS 用。"""
    return [item.strip() for item in (value or "").split(",") if item.strip()]

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
            # 本机文件能力。报的是"工具真的注册了吗"而不是单个开关值——和
            # webSearch 同一个道理：``TOOL_FS_ENABLED`` 开着但用户一个文件夹都
            # 没授权时那六个工具根本不注册，只报开关值会让界面摆出一个点开是空的
            # 文件树。``hasRoots`` 只给真假,不给路径：本机绝对路径是这个用户机器上
            # 的信息,列目录时他自己会看到,没必要出现在一个"能力清单"里。
            "fs": {
                "enabled": fs_tools.enabled(),
                "hasRoots": fs_roots.has_roots(db, current_user.id),
                "writeEnabled": app_settings.TOOL_FS_WRITE_ENABLED,
                "deleteEnabled": app_settings.TOOL_FS_DELETE_ENABLED,
            },
            # 完整能力档案：把散在 .env 里、决定"这个 Agent 到底能做什么"的开关
            # 集中报出来。上面几项只盖了工具/审批/委派/fs，运营方看不到规划、
            # 护栏、记忆、视觉、向量库、备用模型与生效的提示词版本——而这些恰恰是
            # "部署完得到一个纯 RAG 聊天框"还是"一个可用的 Agent"的分界。全部只读：
            # 改这些要改 .env 重启（进程级配置），所以这里报的是"当前生效值"。
            "agent": {
                "planMode": app_settings.AGENT_PLAN_MODE,
                "guardrails": app_settings.GUARDRAIL_ENABLED,
                "memory": app_settings.MEMORY_ENABLED,
                "webFetch": app_settings.TOOL_WEB_FETCH_ENABLED,
                "askUser": app_settings.TOOL_ASK_USER_ENABLED,
                "deleteKnowledge": app_settings.TOOL_DELETE_KNOWLEDGE_ENABLED,
                "visionModels": _split_csv(app_settings.VISION_MODELS),
                # 扫描件 OCR 兜底开着吗。开了但没配视觉模型 / 模型不在白名单时,
                # 这里仍报 true,而"实际能不能跑"由启动日志的 _check_ocr_config 兜——
                # 和 visionModels 只列白名单、不验证端点真的收这些模型是同一种取舍。
                "ocrScanned": app_settings.INGEST_OCR_ENABLED,
                # 向量库后端。memory 是多 worker 不能用的那个（每个 worker 各建一份
                # 索引），报出来让运营方知道当前形态能不能水平扩。
                "vectorStore": app_settings.VECTOR_STORE,
                # 主模型的备用链。空 = 没有降级路径（提供商抽风时主回答直接报错）。
                "fallbackModels": _split_csv(app_settings.LLM_FALLBACK_MODELS),
                # 生效的提示词版本（resolve 后的，不是配置项的字面空串）。
                # 与上面“开了哪些工具”放在一起，运营方才能看出“工具开了但提示词
                # 没讲它们”这类错配（启动日志也会警告，但那只在日志里）。
                "promptVersion": prompt_library.resolve_version("chat_system_rag"),
            },
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
