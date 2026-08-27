"""分块指纹必须覆盖"语料正文变了"这件事。

``ensure_corpus`` 的早退条件是"篇数对上了且没有陈旧文档"，所以指纹是唯一能让
它重建索引的信号。指纹漏掉哪个输入，那个输入的改动就**静默失效**：库里躺着改
动前的分块，报告照常出数，没有任何迹象说明测的是旧语料。

2026-08-23 踩到的就是"语料正文"这一项。把 ``security-policy.md`` 的
``## 账号与口令`` 拆成 ``## 口令强度`` + ``## 认证与账号保护``，实测让
``password-length`` 的 rerank 分从 0.0142 涨到 0.2124（越过原本压着它的
``remote-work.md > 安全`` 的 0.0169）。但只改文件的话 eval 一个数字都不会动——
因为篇数没变、指纹没变，于是早退。

这个文件守的是"指纹的输入集合完整"，而不是某个具体的哈希值。断言哈希常量会
让每次改语料都要更新测试，那种测试只会被人顺手改掉。
"""
from __future__ import annotations

import os

import pytest

from config import settings
from eval import runner


@pytest.fixture
def corpus_file(tmp_path, monkeypatch):
    """把语料目录换到临时目录，避免测试改动真实语料。"""
    directory = tmp_path / "corpus"
    directory.mkdir()
    path = directory / "sample.md"
    path.write_text("# 标题\n\n正文一句。\n", encoding="utf-8")
    monkeypatch.setattr(runner, "CORPUS_DIR", str(directory))
    return path


def test_editing_corpus_body_changes_the_fingerprint(corpus_file):
    """改正文必须换指纹——这正是拆小节那次漏掉的那一项。"""
    before = runner._chunking_fingerprint()
    corpus_file.write_text("# 标题\n\n正文一句。\n\n## 新小节\n\n又一句。\n", encoding="utf-8")
    assert runner._chunking_fingerprint() != before


def test_adding_a_corpus_file_changes_the_fingerprint(corpus_file):
    (corpus_file.parent / "another.md").write_text("# 另一篇\n\n内容。\n", encoding="utf-8")
    before = runner._chunking_fingerprint()
    (corpus_file.parent / "third.md").write_text("# 第三篇\n\n内容。\n", encoding="utf-8")
    assert runner._chunking_fingerprint() != before


def test_renaming_a_corpus_file_changes_the_fingerprint(corpus_file):
    """文件名进摘要：金标按文件名标注 ``expected_documents``，改名等于换了一篇。"""
    before = runner._chunking_fingerprint()
    corpus_file.rename(corpus_file.parent / "renamed.md")
    assert runner._chunking_fingerprint() != before


def test_fingerprint_is_stable_when_nothing_changes(corpus_file):
    """没改动就必须稳定，否则每次跑都全量重嵌。"""
    assert runner._chunking_fingerprint() == runner._chunking_fingerprint()


def test_non_markdown_files_are_ignored(corpus_file):
    """``ensure_corpus`` 只收 ``.md``，摘要的范围必须和它一致。

    不一致的方向都有害：多收会让无关文件触发全量重嵌，少收会让实际入库的内容
    变了而指纹不变。
    """
    before = runner._chunking_fingerprint()
    (corpus_file.parent / "notes.txt").write_text("不参与索引", encoding="utf-8")
    assert runner._chunking_fingerprint() == before


@pytest.mark.parametrize(
    "name,value",
    [
        ("CHUNK_MAX_TOKENS", 96),
        ("CHUNK_OVERLAP_TOKENS", 0),
        ("EMBEDDING_MODEL", "some-other-embedding"),
        ("EVAL_CORPUS_DEGRADE", "dirty-gbk"),
        ("INGEST_CLEAN", False),
    ],
)
def test_config_inputs_still_change_the_fingerprint(corpus_file, monkeypatch, name, value):
    """加了语料摘要之后，原有的配置输入不能失效。"""
    before = runner._chunking_fingerprint()
    assert getattr(settings, name) != value, f"{name} 的对照值和当前值相同，测不出差异"
    monkeypatch.setattr(settings, name, value)
    assert runner._chunking_fingerprint() != before


def test_digest_reads_the_real_corpus_directory():
    """真实语料目录下摘要可计算且非空——tmp_path 测不到路径拼错这类问题。"""
    assert os.path.isdir(runner.CORPUS_DIR)
    assert len(runner._corpus_digest()) == 64
