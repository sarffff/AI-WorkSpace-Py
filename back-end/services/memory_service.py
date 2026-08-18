"""跨会话长期记忆：抽取与注入。

与滚动摘要的分工(见 models.UserMemory 的文档串):摘要回答"这段对话说过
什么",只活在单个会话里;记忆回答"关于这个用户,哪些信息值得在**所有**
以后的对话里知道"——部门、角色、偏好、长期约束。

抽取由辅助模型在每轮回答落库后异步完成(见 chat_router 的触发点),注入
发生在系统提示词之后、对话历史之前。整条链路的原则与检索预取一致:
记忆是增强,不是依赖——抽取失败、解析失败、表为空,任何一环都不影响
本轮回答。
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from config import settings
from models import UserMemory
from services.clock import naive_now

logger = logging.getLogger("memory_service")

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)

# 抽取指令刻意收窄:"值得跨会话记住"不等于"这轮对话重要"。用户问了一次
# 报销流程,那是知识库的事;用户说自己负责报销审核,这才是关于用户的事。
# 注意:这段文本要过 str.format(),JSON 示例的字面量花括号必须写成 {{ }}。
_EXTRACT_INSTRUCTION = (
    "从下面这轮对话里提取\"值得跨会话记住的、关于用户本人的信息\":\n"
    "- 已确认的事实:部门、角色、负责的项目、提到的常用系统或同事\n"
    "- 表达的偏好:语言、格式、沟通方式、明确要求过或禁止过的做法\n"
    "一次性提问的内容、知识库资料、助手自己的回答不算用户信息。\n"
    "\n"
    "没有值得记的就输出 []。只输出 JSON 数组,不要任何解释:\n"
    '[{{"kind": "fact" 或 "preference", "content": "一句话,用第三人称陈述"}}]\n'
    "\n"
    "[用户问题]\n{question}\n\n[助手回答]\n{answer}"
)


def _normalize(text: str) -> str:
    """去重用的归一化:压掉全部空白、统一大小写。

    中文书写没有空格,用户随口多打一个空格不该绕过去重;拉丁短语里的大小写
    差异同理。casefold 对中文无操作,无害。
    """
    return "".join(text.casefold().split())


def _parse_items(raw: str) -> list[dict]:
    """模型输出常裹着解释或代码围栏,只抠出第一个 JSON 数组。"""
    if not raw:
        return []
    match = _JSON_ARRAY_RE.search(raw)
    if not match:
        return []
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


class MemoryService:
    """长期记忆的抽取(写)与注入(读)。"""

    async def extract(
        self,
        model_adapter,
        db: Session,
        *,
        user_id: str,
        chat_id: str | None,
        question: str,
        answer: str,
    ) -> int:
        """从一轮对话抽取记忆,返回新写入的条数。

        幂等靠内容归一化去重:同一句话被反复说出来不该变成两行记忆。
        """
        try:
            completion = await model_adapter.complete(
                messages=[
                    {
                        "role": "user",
                        "content": _EXTRACT_INSTRUCTION.format(
                            question=question[:2000], answer=answer[:4000]
                        ),
                    }
                ],
                tools=[],
                model=settings.utility_model,
                temperature=0.0,
                max_tokens=512,
                purpose="memory_extract",
            )
        except Exception as exc:
            logger.warning("memory extraction call failed: %s", type(exc).__name__)
            return 0

        existing = {
            _normalize(memory.content)
            for memory in db.query(UserMemory)
            .filter(UserMemory.user_id == user_id)
            .all()
        }
        written = 0
        for item in _parse_items(completion.content or ""):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            kind = str(item.get("kind") or "fact").strip()
            if not content or kind not in ("fact", "preference"):
                continue
            # 超长的"记忆"多半是把整段对话抄了一遍,而不是一条可复用的事实
            if len(content) > settings.MEMORY_ITEM_MAX_CHARS:
                continue
            if _normalize(content) in existing:
                continue
            db.add(
                UserMemory(
                    user_id=user_id,
                    kind=kind,
                    content=content,
                    chat_id=chat_id,
                    created_at=naive_now(),
                )
            )
            existing.add(_normalize(content))
            written += 1

        self._prune(db, user_id)
        if written:
            db.commit()
        return written

    def _prune(self, db: Session, user_id: str) -> None:
        """超过上限时丢最旧的记忆。记忆没有"重要性"评分,轮换是诚实的最简策略。"""
        total = (
            db.query(UserMemory)
            .filter(UserMemory.user_id == user_id)
            .count()
        )
        overflow = total - settings.MEMORY_MAX_ITEMS
        if overflow <= 0:
            return
        oldest = (
            db.query(UserMemory)
            .filter(UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at.asc(), UserMemory.id.asc())
            .limit(overflow)
            .all()
        )
        for memory in oldest:
            db.delete(memory)

    def build_system_block(self, db: Session, user_id: str) -> str:
        """注入用的记忆块。最新优先——偏好会变,新说的应该压过旧说的。"""
        memories = (
            db.query(UserMemory)
            .filter(UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at.desc(), UserMemory.id.desc())
            .limit(max(1, settings.MEMORY_INJECT_LIMIT))
            .all()
        )
        if not memories:
            return ""
        lines = [f"- {memory.content}" for memory in memories]
        return "[用户长期记忆(自动从历史对话提取,可能过时)]\n" + "\n".join(lines)

    def list_memories(self, db: Session, user_id: str) -> list[dict]:
        memories = (
            db.query(UserMemory)
            .filter(UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at.desc(), UserMemory.id.desc())
            .all()
        )
        return [
            {
                "id": memory.id,
                "kind": memory.kind,
                "content": memory.content,
                "chatId": memory.chat_id,
                "createdAt": memory.created_at.isoformat(),
            }
            for memory in memories
        ]

    def delete_memory(self, db: Session, memory_id: str, user_id: str) -> bool:
        memory = (
            db.query(UserMemory)
            .filter(UserMemory.id == memory_id, UserMemory.user_id == user_id)
            .first()
        )
        if not memory:
            return False
        db.delete(memory)
        db.commit()
        return True


memory_service = MemoryService()
