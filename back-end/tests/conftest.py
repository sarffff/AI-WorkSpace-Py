"""测试公用替身。

Agent 循环的价值在于编排逻辑本身，所以这里把模型、知识库、数据库全部替换成
可脚本化的替身：测试不触网、不连库，只断言"给定模型行为，循环怎么走"。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

import pytest

from services.model_adapter import (
    ModelAdapter,
    ModelCompletion,
    StreamChunk,
    ToolCall,
)


def run(coro):
    """在同步测试里驱动协程，避免引入 pytest-asyncio 插件依赖。"""
    return asyncio.run(coro)


async def collect(agen: AsyncGenerator[dict[str, Any], None]) -> list[dict[str, Any]]:
    return [event async for event in agen]


class ScriptedAdapter(ModelAdapter):
    """按脚本逐轮回放模型行为，并记录每轮实际收到的 tools 与 messages。

    每个 round spec 支持：
    - ``text``: 本轮流式输出的文本
    - ``tool_calls``: ``[(name, arguments_dict), ...]``
    - ``text_protocol``: 走 GLM 的 ``<function=call>`` 文本工具协议
    - ``protocol_error``: 直接返回协议错误
    - ``raise``: 流式阶段抛异常
    """

    def __init__(self, rounds: list[dict[str, Any]]) -> None:
        self._rounds = list(rounds)
        self.calls: list[dict[str, Any]] = []

    @property
    def rounds_used(self) -> int:
        return len(self.calls)

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
    ) -> ModelCompletion:
        chunks = [
            chunk
            async for chunk in self.stream_completion(
                messages=messages, tools=tools, model=model
            )
        ]
        completion = chunks[-1].completion
        # 非流式调用不会预先透出任何文本,streamed_length 必须为 0。
        completion.streamed_length = 0
        return completion

    async def stream_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        top_p: float = 1.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls.append(
            {
                "tools": [tool["function"]["name"] for tool in tools],
                "messages": [dict(message) for message in messages],
            }
        )
        assert self._rounds, "脚本已用尽：Agent 循环没有按预期终止"
        spec = self._rounds.pop(0)

        if spec.get("raise"):
            raise RuntimeError("simulated stream failure")

        if spec.get("protocol_error"):
            yield StreamChunk(
                completion=ModelCompletion(
                    content="", tool_calls=[], protocol_error="模型返回了无法解析的工具调用格式"
                )
            )
            return

        text = spec.get("text", "")
        streamed = 0
        if text and not spec.get("text_protocol"):
            yield StreamChunk(text=text)
            streamed = len(text)

        calls = [
            ToolCall(
                id=f"call-{index}",
                name=name,
                arguments=json.dumps(arguments, ensure_ascii=False),
            )
            for index, (name, arguments) in enumerate(spec.get("tool_calls", []))
        ]
        if spec.get("text_protocol"):
            yield StreamChunk(
                completion=ModelCompletion(
                    content="",
                    tool_calls=calls,
                    raw_content=text,
                    uses_text_tool_protocol=True,
                )
            )
            return

        yield StreamChunk(
            completion=ModelCompletion(
                content=text, tool_calls=calls, streamed_length=streamed
            )
        )


class FakeKnowledgeService:
    """知识库替身：可控的检索结果，且可让检索通道故障。"""

    def __init__(
        self,
        context: str = "",
        documents: list[dict[str, Any]] | None = None,
        search_fails: bool = False,
        citations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.context = context
        self.documents = documents or []
        self.search_fails = search_fails
        self.citations = citations if citations is not None else []
        self.search_queries: list[str] = []

    async def build_rag_context_with_citations(
        self, db, query, user_id, top_k=5
    ) -> tuple[str, list[dict[str, Any]]]:
        self.search_queries.append(query)
        if self.search_fails:
            raise RuntimeError("embedding api down")
        return self.context, list(self.citations)

    async def build_rag_context(self, db, query, user_id, top_k=5) -> str:
        context, _citations = await self.build_rag_context_with_citations(
            db, query, user_id, top_k
        )
        return context

    async def get_documents(self, db, user_id) -> list[dict[str, Any]]:
        return self.documents

    async def read_chunks(self, db, user_id, document_id, chunk_index, window=1):
        return [
            {"document_name": "notes.md", "chunk_index": chunk_index, "content": "分块正文"}
        ]


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return []

    def first(self):
        return None


class FakeDB:
    """只满足 Agent 循环所需的最小 Session 接口（历史消息查询返回空）。"""

    def query(self, *args, **kwargs):
        return _FakeQuery()


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()
