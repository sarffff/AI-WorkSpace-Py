import json
import threading
from typing import Any

from config import settings
from redis_service import redis_service


DEFAULT_PREFERENCES: dict[str, Any] = {
    "defaultModel": settings.LLM_MODEL,
    "temperature": 0.7,
    "maxTokens": 2048,
    "topP": 1.0,
}

_memory_store: dict[str, dict[str, Any]] = {}
_memory_lock = threading.Lock()


def available_models() -> list[dict[str, str]]:
    """仅对外暴露当前配置提供商支持的那些模型。"""
    base_url = settings.LLM_BASE_URL.lower()
    if "bigmodel.cn" in base_url:
        model_ids = [settings.LLM_MODEL, "glm-4.5-air", "glm-4.6v", "glm-4.7"]
        provider = "智谱 AI"
    elif "deepseek.com" in base_url:
        model_ids = [settings.LLM_MODEL, "deepseek-chat", "deepseek-reasoner"]
        provider = "DeepSeek"
    elif "openai.com" in base_url:
        model_ids = [settings.LLM_MODEL, "gpt-4o", "gpt-4o-mini"]
        provider = "OpenAI"
    else:
        model_ids = [settings.LLM_MODEL]
        provider = "当前 OpenAI 兼容服务"

    unique_ids = list(dict.fromkeys(model_ids))
    return [{"id": model_id, "label": model_id, "provider": provider} for model_id in unique_ids]


def is_model_allowed(model: str) -> bool:
    return model in {item["id"] for item in available_models()}


def _pref_key(user_id: str) -> str:
    return f"ai_workspace:prefs:{user_id}"


def load_preferences(user_id: str) -> dict[str, Any]:
    data = None
    if redis_service.enabled and redis_service.client:
        raw = redis_service.client.get(_pref_key(user_id))
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
    else:
        with _memory_lock:
            data = _memory_store.get(user_id)

    preferences = {**DEFAULT_PREFERENCES, **(data or {})}
    if not is_model_allowed(str(preferences["defaultModel"])):
        preferences["defaultModel"] = settings.LLM_MODEL
    return preferences


def save_preferences(user_id: str, preferences: dict[str, Any]) -> None:
    if redis_service.enabled and redis_service.client:
        redis_service.client.set(
            _pref_key(user_id), json.dumps(preferences, ensure_ascii=False)
        )
    else:
        with _memory_lock:
            _memory_store[user_id] = preferences
