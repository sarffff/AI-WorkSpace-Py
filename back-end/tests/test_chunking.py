"""结构感知 + token 感知的分块。"""
from __future__ import annotations

from services.chunking import split_document
from services.token_budget import HeuristicTokenCounter

COUNTER = HeuristicTokenCounter()

MARKDOWN = """# 安装指南

这是前言段落。

## Windows

先安装 Python。

```bash
# 这行是代码注释，不是标题

pip install -r requirements.txt
```

然后配置环境变量。

## macOS

用 brew 安装即可。
"""


def _chunk(text: str, filename: str = "", **kwargs):
    return split_document(text, filename, counter=COUNTER, **kwargs)


def test_empty_input_produces_no_chunks():
    assert _chunk("") == []
    assert _chunk("   \n\n  ") == []


def test_sections_become_separate_chunks():
    chunks = _chunk(MARKDOWN, "guide.md", max_tokens=60)

    assert [chunk.heading_path for chunk in chunks] == [
        "安装指南",
        "安装指南 > Windows",
        "安装指南 > macOS",
    ]


def test_code_fence_is_never_split_or_read_as_heading():
    chunks = _chunk(MARKDOWN, "guide.md", max_tokens=60)
    windows = next(chunk for chunk in chunks if chunk.heading_path.endswith("Windows"))

    # 围栏内的空行没有把代码块切开，# 开头的注释也没有被当成标题
    assert "pip install -r requirements.txt" in windows.content
    assert "```bash" in windows.content
    assert all("代码注释" not in chunk.heading_path for chunk in chunks)


def test_heading_path_is_prepended_as_context():
    chunks = _chunk(MARKDOWN, "guide.md", max_tokens=60)
    macos = next(chunk for chunk in chunks if chunk.heading_path.endswith("macOS"))

    # 块自带 ## macOS 标题，所以只补祖先路径，不重复标题本身
    assert macos.content.startswith("安装指南\n\n")
    assert macos.content.count("macOS") == 1


def test_section_boundary_wins_over_filling_the_budget():
    """两个极短的小节不会被合并进同一个块。"""
    chunks = _chunk("# A\n\n短。\n\n# B\n\n也短。\n", "x.md", max_tokens=500)

    assert len(chunks) == 2
    assert chunks[0].heading_path == "A"
    assert chunks[1].heading_path == "B"


def test_chunks_respect_the_token_budget():
    long_text = "。".join(f"这是第{index}句话" for index in range(200)) + "。"
    chunks = _chunk(long_text, "notes.txt", max_tokens=80)

    assert len(chunks) > 1
    # 允许少量溢出（重叠回收后再加一个单位），但不能成倍超标
    assert all(COUNTER.count(chunk.content) <= 80 * 2 for chunk in chunks)


def test_oversized_single_line_is_hard_split():
    chunks = _chunk("x" * 4000, "blob.txt", max_tokens=50)

    assert len(chunks) > 1
    assert "".join(chunk.content for chunk in chunks) == "x" * 4000


def test_plain_text_without_headings_splits_by_paragraph():
    text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
    chunks = _chunk(text, "notes.txt", max_tokens=8)

    assert len(chunks) == 3
    assert all(chunk.heading_path == "" for chunk in chunks)


def test_chunk_indexes_are_sequential():
    chunks = _chunk(MARKDOWN, "guide.md", max_tokens=40)

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_unclosed_code_fence_is_not_dropped():
    chunks = _chunk("# T\n\n```python\nprint(1)\n", "x.md", max_tokens=200)

    assert any("print(1)" in chunk.content for chunk in chunks)


def test_deep_heading_path_tracks_ancestors():
    text = "# 一\n\n## 二\n\n### 三\n\n正文。\n"
    chunks = _chunk(text, "x.md", max_tokens=200)

    assert chunks[-1].heading_path == "一 > 二 > 三"
