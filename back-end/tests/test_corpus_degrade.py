"""语料降级：确定性、损伤真的落在词元上、以及哪一类清洗修得了。

这个文件的重点不是"降级函数返回了什么字符串"，而是三个断言：

1. **确定性** —— 降两次逐位相同。不确定的话同一个变体跑两遍得到不同召回，
   变体差异被方差盖掉，整套对照就没意义了（和 agent_runner 把温度钉 0.0 同理）。
2. **损伤真的打到 ``tokenize()`` 上** —— 一个不改变词元的"降级"测不出任何东西，
   会得到"脏数据无害"这个假结论。
3. **哪一类修得了** —— ``noisy_unicode`` 必须能被 ``clean_text`` 修回来（否则
   ``dirty-unicode/+clean`` 那一对量的是 0），而 ``pdf_like`` 必须修不回来
   （否则说明我在纯文本里凭空恢复了几何信息，那不可能，只能是断言写错了）。
"""
from __future__ import annotations

import pytest

from config import settings
from eval import corpus_degrade as cd
from services import ingest_clean as ic
from services.retrieval_index import tokenize

# 这几个词元是金标准里 lexical 探针实际考的东西
_LEXICAL_TOKENS = ("resource_exhausted", "idempotency", "429")

_SOURCE = """# 内部平台 API 指南

## 错误码

| 错误码 | HTTP | 说明 |
| --- | --- | --- |
| `RESOURCE_EXHAUSTED` | 429 | 触发限流 |

所有写接口支持 `Idempotency-Key` 请求头，服务端保留该键 24 小时。

## 认证

请求需带 Bearer Token。令牌有效期为 3600 秒。
"""


def _text(mode: str) -> str:
    payload, _suffix = cd.degrade_corpus_file(_SOURCE, mode)
    return payload.decode("utf-8")


# ========== 确定性 ==========


@pytest.mark.parametrize("mode", cd.DEGRADATIONS)
def test_every_degradation_is_deterministic(mode):
    assert cd.degrade_corpus_file(_SOURCE, mode) == cd.degrade_corpus_file(_SOURCE, mode)


def test_unknown_mode_is_rejected():
    """静默当成 none 处理最糟：报告里写着 dirty-xxx，跑的却是干净语料。"""
    with pytest.raises(ValueError, match="未知的降级方式"):
        cd.degrade_corpus_file(_SOURCE, "nope")


def test_none_is_a_byte_for_byte_passthrough():
    payload, suffix = cd.degrade_corpus_file(_SOURCE, "none")

    assert payload == _SOURCE.encode("utf-8")
    assert suffix == ".md"


# ========== 后缀 ==========


def test_pdf_like_changes_the_suffix():
    """必须换掉 .md：chunking._looks_like_markdown 会看扩展名，
    不换的话"PDF 没有标题层级"这个损伤会被扩展名兜回来，测出来的差值是假的。"""
    assert cd.degrade_corpus_file(_SOURCE, "pdf_like")[1] == ".txt"


def test_encoding_and_unicode_damage_keep_the_suffix():
    """这两类损伤和扩展名无关，换后缀只会引入一个无关变量。"""
    assert cd.degrade_corpus_file(_SOURCE, "gbk_bytes")[1] == ".md"
    assert cd.degrade_corpus_file(_SOURCE, "noisy_unicode")[1] == ".md"


# ========== pdf_like：结构丢失 ==========


def test_pdf_like_strips_heading_markers_but_keeps_the_words():
    """标记必须抹掉（否则损伤不成立），文字必须留着（否则 must_include 不可达，
    量到的就不是"结构丢了"而是"内容删了"）。"""
    dirty = _text("pdf_like")

    assert "# 内部平台" not in dirty
    assert "内部平台 API 指南" in dirty


def test_pdf_like_injects_repeating_running_heads():
    """页码要递增：_strip_running_heads 靠把数字折成 # 才能把
    「第 1 页」和「第 12 页」collapse 成同一个频率键，不递增就测不到那一步。"""
    long_source = _SOURCE + "\n".join(f"第 {n} 条正文" for n in range(40))
    dirty = cd.degrade_corpus_file(long_source, "pdf_like")[0].decode("utf-8")

    assert "公司内部资料" in dirty
    assert "第 1 页" in dirty and "第 2 页" in dirty


def test_pdf_like_removes_blank_lines():
    """chunking._parse_blocks 靠空行 flush 段落；没有空行整章就变成一个大 block。"""
    dirty = _text("pdf_like")

    assert not [line for line in dirty.splitlines() if not line.strip()]


def test_pdf_like_breaks_lexical_tokens():
    dirty_tokens = set(tokenize(_text("pdf_like")))

    assert "resource_exhausted" not in dirty_tokens
    assert "resource_exhausted" in set(tokenize(_SOURCE))


def test_pdf_like_leaves_cjk_alone():
    """真实 PDF 抽取对中文的典型损伤是丢结构而不是插空格，而往中文里插空格会让
    CJK bigram 全废——那是另一类损伤，不该混进这一个变体。"""
    dirty = _text("pdf_like")

    assert "触发限流" in dirty
    assert "服务端保留该键" in dirty


def test_pdf_like_damage_is_not_repairable_by_cleaning():
    """这是 dirty-pdf-like **没有** +clean 对照组的理由。

    词内空格只能靠 PDF 的字号与坐标复原，纯文本里没有几何信息。如果这条断言
    有一天失败了，那不是"清洗变强了"，是有人在 clean_text 里加了猜词边界的逻辑
    ——那会在正常文档上造成误伤。
    """
    repaired = set(tokenize(ic.clean_text(_text("pdf_like"))))

    assert "resource_exhausted" not in repaired


# ========== gbk_bytes：编码 ==========


def test_gbk_bytes_is_really_not_utf8():
    payload, _suffix = cd.degrade_corpus_file(_SOURCE, "gbk_bytes")

    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")
    assert "内部平台" in payload.decode("gb18030")


def test_gbk_bytes_is_recovered_by_sniffing():
    """dirty-gbk / dirty-gbk+clean 那一对的全部差值都在这一步。"""
    payload, _suffix = cd.degrade_corpus_file(_SOURCE, "gbk_bytes")

    assert ic.sniff_decode(payload).text.startswith("# 内部平台")


def test_hard_utf8_decode_destroys_the_sparse_channel():
    """改动前的行为：errors="replace" 之后 tokenize 只剩 ASCII 残留 → BM25 建
    索引时中文一个字都进不去 → 稀疏通道看不见这篇文档，而状态还是 indexed。

    断言改成对着**配置里的真实门槛**比，而不是写死 0.6：这一篇的 ratio 是
    0.6011，在旧门槛 0.6 下差 0.0011 通过，于是被判 indexed。写死数字的话
    "测试红了"和"门槛设得不对"分不开——实际是后者，见 INGEST_MIN_TEXT_RATIO。
    """
    payload, _suffix = cd.degrade_corpus_file(_SOURCE, "gbk_bytes")
    broken = payload.decode("utf-8", errors="replace")

    assert ic.readable_ratio(broken) < settings.INGEST_MIN_TEXT_RATIO
    assert "内部平台" not in broken
    # 这才是"稀疏通道被毁"的直接证据：分词结果全是 ASCII，没有一个中文 token
    assert not any(
        any("一" <= char <= "鿿" for char in token)
        for token in tokenize(broken)
    )


# ========== noisy_unicode：看不见的损伤 ==========


def test_noisy_unicode_damage_lands_on_tokens():
    """屏幕上看起来完全正常，但 ４２９ 在 tokenize 眼里不是 429。"""
    clean_tokens = set(tokenize(_SOURCE))
    dirty_tokens = set(tokenize(_text("noisy_unicode")))

    for token in _LEXICAL_TOKENS:
        assert token in clean_tokens
        assert token not in dirty_tokens


def test_noisy_unicode_is_fully_repairable():
    """这一对的差值全部归 clean_text。修不回来的话 dirty-unicode+clean 量的是 0，
    会得到"清洗没用"这个假结论。"""
    repaired = set(tokenize(ic.clean_text(_text("noisy_unicode"))))

    for token in _LEXICAL_TOKENS:
        assert token in repaired


def test_noisy_unicode_keeps_cjk_readable():
    """全角化只动 ASCII 字母数字。中文标点是正常写法，动它会让这个降级测的
    东西变得不干净。"""
    repaired = ic.clean_text(_text("noisy_unicode"))

    assert "触发限流" in repaired
    assert "服务端保留该键" in repaired


# ========== scanned ==========


def test_scanned_yields_no_text_at_all():
    """它不是用来比召回的（必然是 0），它验证入库自检真的判了 failed。"""
    payload, suffix = cd.degrade_corpus_file(_SOURCE, "scanned")

    assert payload == b""
    assert suffix == ".txt"
