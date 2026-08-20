"""结构感知 + token 感知的分块。"""
from __future__ import annotations

from services.chunking import (
    _percentile,
    adjacent_distances,
    sentences_for_embedding,
    split_document,
    split_semantic,
)
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


# ========== 语义分块 ==========
#
# 这一组验的是"断点判据换了,但对文档的理解没变":标题层级、代码围栏、章节边界
# 三件事必须和 structural 一样成立。只测"距离大的地方断开了"是不够的——真正
# 容易坏的是那三件事在新路径上悄悄失效。

SEMANTIC_DOC = """# 报销政策

## 差旅

员工出差需在五个工作日内提交申请。审批由直属主管完成。财务在三日内打款。

## 招待

招待费用需事先报备。酒水支出不得超过总额百分之三十。

```python
def audit(amount):
    return amount <= LIMIT
```
"""


def _units(text: str = SEMANTIC_DOC, filename: str = "policy.md"):
    return sentences_for_embedding(text, filename)


def test_sentences_carry_heading_path():
    """标题路径必须和 structural 一样带上,否则 contextual chunk 那件事没了。"""
    units = _units()

    body = [unit for unit in units if not unit.atomic]
    assert body
    assert all(unit.heading_path for unit in body)
    assert any(unit.heading_path == "报销政策 > 差旅" for unit in body)


def test_code_fence_stays_one_atomic_unit():
    """按句切代码毫无意义,算它和相邻正文的"语义距离"更没有意义——
    那个距离必然很大,会在每个代码块前后都断一刀。"""
    code_units = [
        unit for unit in _units() if unit.atomic and not unit.heading
    ]

    assert len(code_units) == 1
    assert "def audit" in code_units[0].text
    assert "return amount" in code_units[0].text


def test_headings_are_marked_atomic_and_heading():
    units = _units()

    headings = [unit for unit in units if unit.heading]
    assert len(headings) == 3
    assert all(unit.atomic for unit in headings)


def test_empty_text_yields_no_units():
    assert sentences_for_embedding("", "x.md") == []
    assert sentences_for_embedding("   \n\n ", "x.md") == []


# ---- 距离与分位数 ----


def test_adjacent_distances_length_and_direction():
    distances = adjacent_distances([[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]])

    assert len(distances) == 2
    assert distances[1] > distances[0]  # 正交向量距离更大


def test_adjacent_distances_handles_degenerate_vectors():
    """零向量、维度不一致都不该抛——入库时一个坏向量不该让整篇文档进不了库。"""
    assert adjacent_distances([]) == []
    assert adjacent_distances([[1.0, 0.0]]) == []
    assert adjacent_distances([[0.0, 0.0], [1.0, 0.0]]) == [1.0]
    assert adjacent_distances([[1.0], [1.0, 0.0]]) == [1.0]


def test_percentile_is_interpolated():
    assert _percentile([], 95) == float("inf")
    assert _percentile([0.5], 95) == 0.5
    assert _percentile([0.0, 1.0], 50) == 0.5
    assert _percentile([0.0, 0.5, 1.0], 100) == 1.0


def test_empty_distances_threshold_never_breaks():
    """没有可统计的距离时阈值是 inf,于是不会因为距离而断开——
    退化成"只按标题和预算断",也就是 structural 的行为。"""
    units = _units()
    chunks = split_semantic(units, [], max_tokens=4000, counter=COUNTER)

    assert chunks


# ---- 组块 ----


def _fake_vectors(units, hot: str) -> list[list[float]]:
    """含 ``hot`` 的单位给一个正交向量,于是它前面必然是一个大距离。"""
    return [[1.0, 0.0] if hot in unit.text else [0.0, 1.0] for unit in units]


def test_section_boundary_still_wins():
    """同一个块内的内容一定来自同一节——这条在 structural 里是承诺,
    换了断点判据之后必须仍然成立。"""
    units = _units()
    distances = adjacent_distances(_fake_vectors(units, "招待"))
    chunks = split_semantic(units, distances, max_tokens=4000, counter=COUNTER)

    assert not any(
        "员工出差" in chunk.content and "招待费用" in chunk.content for chunk in chunks
    )


def test_chunk_indexes_are_contiguous():
    """index 是邻域扩展的定位依据,不连续会让 read_document_chunk 取错分块。"""
    units = _units()
    chunks = split_semantic(
        units, adjacent_distances(_fake_vectors(units, "酒水")), counter=COUNTER
    )

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_token_budget_is_enforced_even_when_topic_holds():
    """语义距离只说明"话题没转",不说明"这段塞得进上下文"。
    少了这条兜底,一篇话题连贯的长文会变成一个巨块。"""
    long_text = "这是一句连贯的说明文字。" * 200
    units = sentences_for_embedding(long_text, "long.md")
    # 全部向量相同 → 距离全 0 → 没有任何语义断点
    vectors = [[1.0, 0.0] for _unit in units]
    chunks = split_semantic(
        units, adjacent_distances(vectors), max_tokens=120, counter=COUNTER
    )

    assert len(chunks) > 1
    assert all(COUNTER.count(chunk.content) <= 120 for chunk in chunks)


def test_semantic_break_splits_within_a_section():
    """这是语义分块真正新增的那一条:同一节内部也能按话题断开。"""
    text = "## 一节\n\n甲话题第一句。甲话题第二句。乙话题第一句。乙话题第二句。\n"
    units = sentences_for_embedding(text, "x.md")
    body = [unit for unit in units if not unit.atomic]
    assert len(body) >= 4

    vectors = [
        [1.0, 0.0] if "甲" in unit.text else [0.0, 1.0] for unit in units
    ]
    chunks = split_semantic(
        units,
        adjacent_distances(vectors),
        max_tokens=4000,
        percentile=50.0,
        counter=COUNTER,
    )

    assert len(chunks) > 1
    assert not any("甲话题第一句" in c.content and "乙话题第二句" in c.content for c in chunks)


def test_no_units_yields_no_chunks():
    assert split_semantic([], [], counter=COUNTER) == []
