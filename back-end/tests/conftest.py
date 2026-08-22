"""测试公用替身。

Agent 循环的价值在于编排逻辑本身，所以这里把模型、知识库、数据库全部替换成
可脚本化的替身：测试不触网、不连库，只断言"给定模型行为，循环怎么走"。

功能开关一律由 ``_pin_feature_flags`` 钉死，不继承本地 ``.env``——见那个 fixture
的说明。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

import pytest

from config import settings
from services.model_adapter import (
    ModelAdapter,
    ModelCompletion,
    StreamChunk,
    ToolCall,
)

# 需要钉住的开关，以及测试假定的值。
#
# 为什么必须钉：``settings`` 是从 .env 读的，测试进程会继承开发机上的真实配置。
# 开了 AGENT_APPROVAL_MODE=write 之后，写知识库的三条测试立刻失败——工具调用停在
# 审批门前，断言看到的是"没有写入"，而报错信息是 `assert 0 == 1`，完全没提审批。
# 这和评估里 _BASE 必须钉 AGENT_APPROVAL_MODE 是同一个坑的两个入口：功能开关
# 默认关闭时没人发现，一旦有人在本地打开，测试就开始报与改动无关的失败。
#
# 需要测另一个取值的测试自己 monkeypatch——那样意图写在测试里，而不是隐含在
# 谁的 .env 里。
_PINNED_FLAGS = {
    # 审批门会让工具调用在 tool_start 之前中断，断言"工具执行了什么"的测试全部受影响
    "AGENT_APPROVAL_MODE": "off",
    # 检查点会额外落库，且 resume 路径有自己的测试
    "AGENT_CHECKPOINT_ENABLED": False,
    # 全部钉成 config.py 里的代码默认值，而不是"测试想要的值"。
    #
    # 这一条踩过：先按"写知识库的测试需要它"钉成 True，结果
    # test_new_tools_not_registered_by_default 挂了——那条断言的恰恰是"没开开关
    # 就不注册"，把开关钉开等于把它要测的前提抹掉。真正需要工具面的测试自己
    # monkeypatch（见 test_service_security._enable_save_tool），它们缺的只是
    # 上面那个审批开关。
    "TOOL_WRITE_KNOWLEDGE_ENABLED": False,
    # 提示词版本。空串 = 走代码里的默认版本；.env 里设成 v4-workspace 之后，
    # 断言系统提示词内容的测试会挂在与改动无关的地方（v4 里没有"不要重复检索"
    # 那句，它属于 v2/v3-lean）。提示词版本和工具面是耦合的，一旦本地为了试新
    # 工具切了版本，这类断言就全部漂移。
    "PROMPT_CHAT_SYSTEM_VERSION": "",
    # 护栏按"开启但不拦截"测，拦截行为由 test_guardrails 自己 monkeypatch 阈值
    "GUARDRAIL_ENABLED": True,
    "GUARDRAIL_BLOCK_SCORE": 0,
}


@pytest.fixture(autouse=True)
def _pin_feature_flags(monkeypatch):
    """把功能开关钉到测试假定的值，隔离本地 .env。

    autouse：漏掉一个测试就会重新引入"结果取决于谁的 .env"这件事。
    只钉 ``_PINNED_FLAGS`` 里列出的键，其余配置照旧从环境读。
    """
    for name, value in _PINNED_FLAGS.items():
        if hasattr(settings, name):
            monkeypatch.setattr(settings, name, value)


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
    - ``raise_after_text``: 先流出 ``text`` 再抛异常（模拟回答说到一半断流）
    - ``finish_reason``: 提供商回传的终止原因。省略时按有无文本推断
      ``stop``——只有要造"撞到 max_tokens"的场景才需要显式写 ``"length"``，
      而 ``{"text": "", "finish_reason": "length"}`` 就是推理模型把预算花在
      思考上、一个字都没吐的那个形状（记忆抽取曾经 100% 落在这里）。
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
                messages=messages,
                tools=tools,
                model=model,
                temperature=temperature,
                # 必须往下传：不传的话 stream_completion 记下的是它自己的默认值，
                # 而"调用方给了多少输出预算"是能量出真问题的——记忆抽取就因为
                # 这个数写死得太小而 100% 静默失效过。
                max_tokens=max_tokens,
                top_p=top_p,
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
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        assert self._rounds, "脚本已用尽：Agent 循环没有按预期终止"
        spec = self._rounds.pop(0)

        if spec.get("raise"):
            raise RuntimeError("simulated stream failure")

        if spec.get("raise_after_text"):
            text = spec.get("text", "")
            if text:
                yield StreamChunk(text=text)
            raise RuntimeError("simulated stream failure after text")

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
        finish_reason = spec.get("finish_reason", "stop")
        if spec.get("text_protocol"):
            yield StreamChunk(
                completion=ModelCompletion(
                    content="",
                    tool_calls=calls,
                    raw_content=text,
                    uses_text_tool_protocol=True,
                    finish_reason=finish_reason,
                )
            )
            return

        yield StreamChunk(
            completion=ModelCompletion(
                content=text,
                tool_calls=calls,
                streamed_length=streamed,
                finish_reason=finish_reason,
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
    """只满足 Agent 循环所需的最小 Session 接口（历史消息查询返回空）。

    ``add`` / ``commit`` 是空操作:工具轨迹落库在循环里是顺带发生的,用这个替身
    的测试关心的是事件流与 messages,不是持久化。真要断言落库就用 ``db_real``。
    """

    def query(self, *args, **kwargs):
        return _FakeQuery()

    def add(self, *args, **kwargs):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


@pytest.fixture
def db_real():
    """真正建表的内存 SQLite session。

    反馈那套逻辑的重点是唯一约束、越权检查和聚合查询——用 FakeDB 全测不出来。
    SQLite 与 MySQL 在这些行为上一致，够用；真正依赖 MySQL 方言的地方另说。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database import Base
    import models  # noqa: F401  确保所有表都已注册到 Base.metadata

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_chat(session, *, user_id: str = "u1"):
    from models import Chat, Message
    from services.clock import naive_now

    now = naive_now()
    chat = Chat(id="c1", user_id=user_id, title="测试会话", created_at=now, updated_at=now)
    session.add(chat)
    question = Message(
        id="m-user", chat_id="c1", role="user", content="试用期多久？", created_at=now
    )
    session.add(question)
    session.commit()
    return chat, question


@pytest.fixture
def chat_with_question(db_real) -> tuple[str, str]:
    _chat, question = _seed_chat(db_real)
    return "c1", question.id


@pytest.fixture
def chat_with_answer(db_real) -> tuple[str, str]:
    from models import Message
    from services.clock import naive_now

    _seed_chat(db_real)
    answer = Message(
        id="m-assistant",
        chat_id="c1",
        role="assistant",
        content="试用期 6 个月。",
        model="glm-4.5-air",
        created_at=naive_now(),
    )
    db_real.add(answer)
    db_real.commit()
    return "c1", answer.id
