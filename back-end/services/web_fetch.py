"""URL 抓取：政策文档入库的一条来源通道。

只做两件事：把一个 URL 取成原始 HTML，以及把一个模型/用户给的标题变成安全的
文档名。抓取的所有护栏都在 ``services.egress`` 那一层（SSRF 判据、逐跳重定向校验、
大小与超时上限），这里不重复一遍。

不做的事：剥标签、过护栏、截断正文——那是入库链路自己的决定
（``ingest_clean.html_to_text_structured`` 保留块结构，清洗走 ``INGEST_CLEAN``）。
也没有"给模型当工具用的网页搜索/网页读取"：这个 Agent 的答案来源是政策库与
历史工单，不是任意网页。
"""
from __future__ import annotations

import re

import httpx

from config import settings
from services import egress

_MAX_REDIRECTS = 5

# 文档名里允许的字符：字词、空白、点、横杠，以及中日韩汉字段。
_UNSAFE_NAME = re.compile(r"[^\w一-鿿 .\-]+")
_DOT_RUN = re.compile(r"\.{2,}")


class FetchError(Exception):
    """抓取失败（超限、状态码、解码等），消息可直接展示。"""


async def _http_get_text(url: str, max_bytes: int, timeout: float) -> str:
    """抓取一个 URL 的正文并解码。手动跟随重定向：每一跳都先过 egress.check_url，
    否则白名单内页面 302 到内网/元数据就绕过了 SSRF 防护。测试可替换传输层。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; AI-Workspace/1.0; "
            "+https://github.com/anomalyco/opencode)"
        )
    }
    current = url
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout, headers=headers
    ) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            await egress.check_url(current)
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("重定向响应缺少 Location 头。")
                    current = str(response.url.join(location))
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f"页面超过 {max_bytes} 字节上限，未完整读取。")
                    chunks.append(chunk)
                break
        else:
            raise FetchError(f"重定向超过 {_MAX_REDIRECTS} 次上限。")
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        try:
            return b"".join(chunks).decode("gb18030")
        except UnicodeDecodeError as exc:
            raise FetchError("页面不是可识别的文本编码（utf-8/gb18030）。") from exc


async def fetch_page_html(url: str) -> str:
    """抓取一个 URL 的**原始 HTML**（egress 防护 + 大小/超时上限）。

    失败抛 ``egress.EgressBlocked`` 或 ``FetchError``，由调用方转成可读提示。
    """
    return await _http_get_text(
        url, settings.WEB_FETCH_MAX_BYTES, settings.WEB_FETCH_TIMEOUT_SECONDS
    )


def safe_document_name(raw: str) -> str:
    """把模型给的标题变成一个规整的文档名。

    这个名字只进数据库的 ``documents.name`` 列，不落文件系统，所以这里不是在防
    路径穿越；要防的是**观感上的歧义**：文件名本身就是注入面——一个叫
    ``【参考 9】忽略以上指令.md`` 的文档，光是出现在文档列表里就足以伪造出一条
    参考资料。所以除了去掉控制字符与分隔符，还要把 ``..`` 这类看起来像路径的
    残留折掉。
    """
    cleaned = _UNSAFE_NAME.sub(" ", (raw or "").strip())
    cleaned = _DOT_RUN.sub(".", cleaned)
    cleaned = " ".join(cleaned.split()).strip(". ")[:80]
    return cleaned or "未命名笔记"
