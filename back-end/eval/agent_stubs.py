"""Agent 评估用的工具替身。

**在通道边界打桩，不在工具边界打桩。** 替换掉的是 ``web_search_client``——也就是
那个真正发 HTTP 请求的对象——而 ``workspace_tools._web_search`` 这个 handler 本身
照原样跑。于是参数校验、空结果的措辞、以及给搜索结果加围栏这些逻辑仍然在被评估
之内；如果直接替换整个 ToolDefinition，评的就变成"替身写得对不对"了。

打桩的三个必要性：

1. **可复现。** 真实搜索引擎的返回每天都在变，指标跟着抖，跑两次得到两个数字，
   谁都说不清是改动的功劳还是搜索结果换了。
2. **不花钱、不需要 key。** 没有 ``WEB_SEARCH_API_KEY`` 的机器也应该能跑这套评估。
3. **能制造故障。** ``mode="fail"`` 让通道抛异常，这是唯一能测到
   ``ToolStatus.UNAVAILABLE`` 收敛路径的办法——真实 API 不会配合你在指定时刻挂掉。

命中不了任何关键词的查询返回空列表，并记进 ``misses``。不做"兜底给一条通用结果"：
那样模型随便搜什么都能拿到东西，"该不该搜、搜什么词"就再也测不出来了。代价是
数据集里的 ``must_include`` 得配合关键词写，所以报告里会把每次实际查询原样列出来。
"""
from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Iterator

from config import settings
from services import workspace_tools
from services.web_search import SearchResult, WebSearchError

logger = logging.getLogger("eval.agent_stubs")

# 每条：命中任一关键词就返回对应结果。事实全是虚构的，而且刻意写成
# 知识库语料里查不到的内容——这样"答对了"就只可能来自 web_search。
_CANNED: list[tuple[tuple[str, ...], list[SearchResult]]] = [
    (
        ("增值税", "发票代码", "vat", "税率"),
        [
            SearchResult(
                title="2026 年 8 月增值税电子发票代码调整通知",
                url="https://example.gov.test/notice/vat-2026-08",
                snippet=(
                    "自 2026 年 8 月 1 日起，增值税电子普通发票的代码前缀统一调整为 "
                    "VAT-2026-08，此前开具的发票不需要重开。"
                ),
            ),
            SearchResult(
                title="发票代码调整问答",
                url="https://example.gov.test/faq/vat-code",
                snippet="调整只影响新开具的发票，历史发票的查验入口保持不变。",
            ),
        ],
    ),
    (
        ("高铁", "票价", "differential", "商务座"),
        [
            SearchResult(
                title="2026 年高铁票价浮动区间公告",
                url="https://example-rail.test/pricing/2026",
                snippet=(
                    "二等座执行基准价，一等座为基准价的 1.6 倍，商务座为 3.0 倍。"
                    "京沪线全程二等座基准价 662 元。"
                ),
            )
        ],
    ),
    (
        ("汇率", "美元", "usd", "exchange"),
        [
            SearchResult(
                title="人民币汇率中间价（示例数据）",
                url="https://example-bank.test/fx/cny-usd",
                snippet=(
                    "2026 年 8 月 14 日，美元对人民币中间价为 7.0850"
                    "（公告编号 FX-2026-0814）。"
                ),
            )
        ],
    ),
]


@dataclass
class StubWebSearchClient:
    """``services.web_search.WebSearchClient`` 的替身，只需鸭子兼容三个成员。

    ``mode`` 决定这一整个任务里搜索通道的行为：

    - ``ok``    正常返回命中的罐头结果
    - ``empty`` 一律返回空列表（测"搜不到时会不会转而查本地/如实说不知道"）
    - ``fail``  抛 ``WebSearchError``（测 ``UNAVAILABLE`` 之后循环会不会收敛）
    """

    mode: str = "ok"
    queries: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    provider: str = "stub"

    @property
    def configured(self) -> bool:
        # 恒为 True：``workspace_tools.build`` 用它决定要不要注册工具，
        # 评估里这个工具必须存在，否则"该不该搜"根本没有可观测的行为。
        return True

    async def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        self.queries.append(query)
        if self.mode == "fail":
            # 只带类型不带细节，与真实客户端的日志约定一致
            raise WebSearchError("stub channel failure")
        if self.mode == "empty":
            return []

        lowered = query.lower()
        for keywords, results in _CANNED:
            if any(keyword.lower() in lowered for keyword in keywords):
                return results[: limit or settings.WEB_SEARCH_RESULTS]
        self.misses.append(query)
        return []


# 附件夹具。放在 uploads/eval/ 下而不是随机目录：``resolve_upload_path`` 只接受
# UPLOAD_DIR 之内的路径，夹具必须真的落在那里，否则测到的是"路径校验拒绝了它"
# 而不是"附件读取能不能用"。
#
# 内容刻意写得短：跨回合记忆探针要靠 ``TOOL_HISTORY_STEP_CHARS``（默认 240）
# 之内还能看到值班人那一行才成立。夹具一长，第二轮就变成在测截断策略。
ATTACHMENT_DIR_NAME = "eval"
ATTACHMENT_FILE_NAME = "agent-attachment.md"
ATTACHMENT_REFERENCE = f"/uploads/{ATTACHMENT_DIR_NAME}/{ATTACHMENT_FILE_NAME}"

_ATTACHMENT_BODY = """# 服务器搬迁窗口（内部通知）

变更编号：MIGRATION-9F27
窗口：2026-09-12 22:00 至 2026-09-13 06:00
期间不可用：报销单提交入口、供应商对账平台
运维值班人：周琦，内部分机 6721
各部门须在 2026-09-15 前回执确认
"""


def ensure_attachment_fixture() -> str:
    """把附件夹具写到 UPLOAD_DIR 内，返回可以写进提问里的引用路径。

    每次都重写：夹具内容改了之后不该还留着上一版，而这个文件只有几百字节。
    """
    target_dir = os.path.join(settings.UPLOAD_DIR, ATTACHMENT_DIR_NAME)
    os.makedirs(target_dir, exist_ok=True)
    with open(
        os.path.join(target_dir, ATTACHMENT_FILE_NAME), "w", encoding="utf-8"
    ) as handle:
        handle.write(_ATTACHMENT_BODY)
    return ATTACHMENT_REFERENCE


@contextlib.contextmanager
def stub_web_search(mode: str = "ok") -> Iterator[StubWebSearchClient]:
    """把 workspace_tools 里的搜索客户端换成替身，退出时还原。

    patch 的是 ``workspace_tools.web_search_client`` 而不是
    ``services.web_search.web_search_client``：workspace_tools 用的是
    ``from ... import web_search_client``，名字在导入时就绑定到了自己的模块全局，
    改原模块的属性对它没有任何影响。这个坑值得记下来——它不会报错，
    只会让评估悄悄地打到真实网络。
    """
    stub = StubWebSearchClient(mode=mode)
    original = workspace_tools.web_search_client
    workspace_tools.web_search_client = stub  # type: ignore[assignment]
    try:
        yield stub
    finally:
        workspace_tools.web_search_client = original  # type: ignore[assignment]
