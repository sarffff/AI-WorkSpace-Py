"""金标关键词的完整性守卫。

这不是在测产品代码，是在测**数据集**——但它和产品测试一样值得存在，因为
``keyword_coverage`` 的失效方式是静默的：关键词选错了不会报错，只会让报告里
的数字失去意义，而结论已经被读走了。

守两件事：

1. ``must_include`` 在 ``expected_documents`` 里折叠后真实存在。找不到的话这条
   题的覆盖率恒为 0，报告上表现为"模型答不出来"——尺子坏了却像被测对象坏了。
2. 关键词不被同篇文档里更长的数字整体包含。裸数字 ``"5"`` 会落在 ``"15 天"``
   里，于是**答错也算满分**。

第 2 条是这个文件存在的真正理由。2026-08-23 往语料里加了 7 篇文档，其中
``remote-work.md`` 写了"核心协作时段 10:00 至 16:00"，与 ``hr-handbook.md`` 的
"核心工作时间 10:00 至 16:00" 撞车，把一条老题的 nDCG 从 1.0 打到 0.63。
**加语料能打破既有用例**，而金标里还有 19 个"目前不冲突"的裸数字。它们哪天被
新语料打破，这条测试会当场失败；没有它就只能等到某次报告的数字看起来不对时
才有人回头查。
"""
from __future__ import annotations

import glob
import json
import os
import re

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(_BACKEND, "eval", "datasets", "rag_golden.jsonl")
CORPUS_DIR = os.path.join(_BACKEND, "eval", "corpus")


def _fold(text: str) -> str:
    """与 eval/metrics.py 的 _fold 一致。不一致的话这里测的就不是实际行为。"""
    return "".join(text.lower().split())


@pytest.fixture(scope="module")
def corpus() -> dict[str, str]:
    return {
        os.path.basename(path): open(path, encoding="utf-8").read()
        for path in glob.glob(os.path.join(CORPUS_DIR, "*.md"))
    }


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    with open(GOLDEN, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_fold_matches_metrics_implementation():
    """本地 _fold 与 metrics 的实现必须等价，否则这个文件的守卫是假的。"""
    from eval.metrics import _fold as metrics_fold

    for probe in ("3 个工作日", "  HMAC ", "30%", "结转 5 天", ""):
        assert _fold(probe) == metrics_fold(probe)


def test_every_keyword_exists_in_its_expected_documents(cases, corpus):
    missing: list[str] = []
    for case in cases:
        docs = case.get("expected_documents") or []
        for keyword in case.get("must_include") or []:
            folded = _fold(keyword)
            if not any(folded in _fold(corpus.get(name, "")) for name in docs):
                missing.append(f"{case['id']}: {keyword!r} 不在 {docs}")
    assert not missing, "这些关键词在期望文档里找不到，覆盖率会恒为 0:\n" + "\n".join(
        missing
    )


def test_no_keyword_is_swallowed_by_a_longer_number(cases, corpus):
    """关键词命中位置的前一个字符不能是数字。

    前面紧跟数字说明它只是更长数字的尾部：``"5 天"`` 落在 ``"15天"`` 里、
    ``"30"`` 落在 ``"300元"`` 里。这种命中让错答案拿满分。
    """
    swallowed: list[str] = []
    for case in cases:
        docs = case.get("expected_documents") or []
        for keyword in case.get("must_include") or []:
            folded = _fold(keyword)
            for name in docs:
                body = _fold(corpus.get(name, ""))
                for match in re.finditer(re.escape(folded), body):
                    if match.start() > 0 and body[match.start() - 1].isdigit():
                        context = body[max(0, match.start() - 6) : match.end() + 2]
                        swallowed.append(
                            f"{case['id']}: {keyword!r} 被更长数字包含 "
                            f"@{name} 附近 {context!r}"
                        )
                        break
    assert not swallowed, "这些关键词会让错答案算满分:\n" + "\n".join(swallowed)


def test_expected_documents_all_exist(cases, corpus):
    """期望文档必须是真实文件。写错文件名会让 recall 恒为 0。"""
    unknown: list[str] = []
    for case in cases:
        for name in case.get("expected_documents") or []:
            if name not in corpus:
                unknown.append(f"{case['id']}: {name}")
    assert not unknown, "expected_documents 指向不存在的语料:\n" + "\n".join(unknown)


def test_ids_are_unique(cases):
    ids = [case["id"] for case in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"金标 id 重复: {sorted(duplicates)}"


def test_answerable_cases_name_their_source(cases):
    """answerable=True 必须给出 expected_documents，否则 ranking 指标无从计算。"""
    bad = [
        case["id"]
        for case in cases
        if case.get("answerable") and not case.get("expected_documents")
    ]
    assert not bad, f"这些题声称可回答但没标来源: {bad}"
