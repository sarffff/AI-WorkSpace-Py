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

from config import Settings, settings
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
# ``TOOL_* / AGENT_* / CLARIFY_*`` 整族**不在这个字典里逐个列**，由
# ``_FEATURE_FLAG_PREFIXES`` 统一钉成 config.py 的代码默认值。
#
# 为什么改成整族：这个坑在这个文件里已经记过两次（最初的 AGENT_APPROVAL_MODE，
# 2026-08-24 又补 TOOL_CALCULATE / TOOL_WEB_SEARCH），2026-09-05 打开
# read_attachment / delete_knowledge / ask_user / fetch_web_page 之后它第三次
# 出现，红的又是同一批与改动无关的测试。逐个补的做法保证了"下次开一个新开关，
# 再红一次"——而开关只会越来越多。
#
# 钉成**代码默认值**而不是"测试想要的值"：这一条踩过。先按"写知识库的测试需要它"
# 把 TOOL_WRITE_KNOWLEDGE 钉成 True，结果那条断言"没开开关就不注册"的测试挂了——
# 把开关钉开等于把它要测的前提抹掉。真正需要工具面的测试自己 monkeypatch
# （见 test_service_security._enable_save_tool）。
_PINNED_FLAGS = {
    # 这两个是**字符串**型的开关，按类型钉 bool 的那条规则覆盖不到它们。
    #
    # ``AGENT_APPROVAL_MODE=write`` 会让工具调用停在审批门前，断言"工具执行了什么"
    # 的测试全部受影响，而报错是 `assert 0 == 1`，完全没提审批。
    # ``AGENT_PLAN_MODE=plan_execute`` 会在每个回合前多插一次辅助模型调用，
    # 把 ScriptedAdapter 的剧本错位一格——那时"第一轮"拿到的是规划那次的脚本。
    "AGENT_APPROVAL_MODE": "off",
    "AGENT_PLAN_MODE": "off",
    # 2026-08-28 补：模型名也得钉。``chat_service`` 到处是
    # ``model or settings.LLM_MODEL``，所以任何不显式传模型的测试，实际参与
    # 判断的都是本地 .env 里的模型名。视觉那两条测试就是这么红的：白名单写
    # ``glm-4.5-air``，而 .env 是 ``glm-4.6v``，于是断言"图片变成内容块"必然失败，
    # 报错却长得像视觉功能坏了。钉成一个**不在任何视觉白名单里**的固定名字，
    # 需要视觉的测试自己 monkeypatch 成白名单里的值。
    "LLM_MODEL": "glm-4.5-air",
    "VISION_MODELS": "",
    # 提示词版本。空串 = 走代码里的默认版本；.env 里设成 v4-workspace 之后，
    # 断言系统提示词内容的测试会挂在与改动无关的地方（v4 里没有"不要重复检索"
    # 那句，它属于 v2/v3-lean）。提示词版本和工具面是耦合的，一旦本地为了试新
    # 工具切了版本，这类断言就全部漂移。
    "PROMPT_CHAT_SYSTEM_VERSION": "",
    # 护栏按"开启但不拦截"测，拦截行为由 test_guardrails 自己 monkeypatch 阈值
    "GUARDRAIL_ENABLED": True,
    "GUARDRAIL_BLOCK_SCORE": 0,
    # 2026-08-29 补：用量闸门。它按**用户**计数（不是按 IP，见 usage_guard 的
    # 模块文档），而测试里同一个用户会连发几十次请求，20/min 的默认值必然撞上。
    # 撞上之后的症状是 429，而报错位置在被测功能里，看起来像那个功能坏了——
    # 与上面 LLM_MODEL 那条完全同一个形状。
    #
    # 关掉而不是把上限调高：闸门自己的行为由 test_usage_guard 显式打开来测，
    # 其余测试不该有一个"跑够多少次就会红"的隐含前提。
    "USAGE_GUARD_ENABLED": False,
}

# 整族钉住的前缀。这些是"打开一个能力"的开关，而能力一旦打开就会改变工具面、
# 中断行为或轮次结构——也就是最容易让无关测试变红的那一类配置。
#
# 取值来自 ``Settings.model_fields[name].default``，即**类定义里写的默认值**，
# 完全不经过 .env。不用新建一个 ``Settings()`` 实例来取：pydantic-settings 的
# 构造过程本身就会去读 .env，那样取回来的还是开发机上的值。
# 按**类型**而不是按名字前缀：``bool`` 型配置就是功能开关，这是一条不依赖命名的
# 判据。
#
# 前缀那版（``TOOL_ / AGENT_ / CLARIFY_``）在 2026-09-05 加 skill 时第四次踩到同一个
# 坑——``SKILL_ENABLED`` 是个新前缀，于是它读的还是本地 .env，三条与 skill 毫无关系
# 的测试变红（断言"不开 RAG 时不下发工具"之类）。前缀清单保证了"下次再加一族新开关，
# 再红一次"，而开关只会越来越多。
#
# 取值来自 ``Settings.model_fields[name].default``，即**类定义里写的默认值**，
# 完全不经过 .env。不用新建一个 ``Settings()`` 实例来取：pydantic-settings 的构造
# 过程本身就会去读 .env，那样取回来的还是开发机上的值。
def _code_default_flags() -> dict[str, object]:
    return {
        name: field.default
        for name, field in Settings.model_fields.items()
        if isinstance(field.default, bool)
    }


@pytest.fixture(autouse=True)
def _pin_feature_flags(monkeypatch):
    """把功能开关钉到测试假定的值，隔离本地 .env。

    autouse：漏掉一个测试就会重新引入"结果取决于谁的 .env"这件事。

    两层，后者覆盖前者：

    1. **所有 bool 型配置** → config.py 的代码默认值。按类型判定，所以新加的开关
       自动被覆盖，不必记得来这里登记一次。
    2. ``_PINNED_FLAGS`` → 需要偏离代码默认值的那几个（模型名、提示词版本、
       用量闸门、以及 ``AGENT_APPROVAL_MODE`` 这类非 bool 的开关）。

    需要测另一个取值的测试自己 monkeypatch，那样意图写在测试里。
    """
    for name, value in {**_code_default_flags(), **_PINNED_FLAGS}.items():
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
        purpose: str = "chat",
    ) -> ModelCompletion:
        """``purpose`` 必须收下，即使这里用不上它。

        所有走 ``structured.request_structured`` 的辅助调用（规划、记忆抽取、
        指代消解、历史摘要）都会传这个参数。不收的话那些调用一律 TypeError，
        而调用方**普遍会把异常吞成"没产出"然后静默降级**——于是测试看到的是
        "规划没发生"，报错信息里一个字都不提签名不匹配。

        2026-09-05 发现：``AGENT_PLAN_MODE=plan_execute`` 打开之后，除了
        test_agent_loop 里那两条用了本地子类的用例，**所有集成测试里的规划路径
        从来没被真正走过**——每次都是 TypeError → 退回纯 ReAct → 断言照样通过。
        """
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
        # 每次检索传进来的 viewer_id。None 表示"只查共享文档"，
        # 而在真实 chat 链路上它应当**总是**当前用户 id。
        self.viewer_ids: list[str | None] = []

    # viewer_id 收下并记下来：真实实现用它决定"能不能检索到这个人的私有文档"，
    # 而漏传它是个静默的功能缺失（模型永远看不见用户的个人资料）。
    # 替身把它记进 viewer_ids，于是想断言"链路有没有把它传下去"的测试有东西可断。
    async def build_rag_context_with_citations(
        self, db, query, workspace_id, top_k=5, viewer_id=None
    ) -> tuple[str, list[dict[str, Any]]]:
        self.search_queries.append(query)
        self.viewer_ids.append(viewer_id)
        if self.search_fails:
            raise RuntimeError("embedding api down")
        return self.context, list(self.citations)

    async def build_rag_context(
        self, db, query, workspace_id, top_k=5, viewer_id=None
    ) -> str:
        context, _citations = await self.build_rag_context_with_citations(
            db, query, workspace_id, top_k, viewer_id=viewer_id
        )
        return context

    async def get_documents(self, db, workspace_id, viewer_id=None) -> list[dict[str, Any]]:
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
