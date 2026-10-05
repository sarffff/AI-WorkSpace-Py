"""参数摘要的测试。

审批、审计链与工单轨迹共用同一套摘要算法，所以这里钉的是那个**口径**本身：

- digest 按 key 排序（否则同一份参数算出两个哈希，"批的和执行的是同一份"会假阴性）
- digest 对内容敏感（改了一个字就要看得出来）
- 预览截断但写出原始长度（20000 字的写入和 200 字的写入是两件不同的事）
- 不可序列化的参数不抛异常（摘要是记录，不是闸门）
"""
from __future__ import annotations

from services import approval_audit


def test_digest按key排序():
    """键序不同的同一份参数必须算出同一个哈希。

    不排序的话，"批准的和执行的是不是同一份"这个判断会假阴性——而那是这条链
    唯一真正要回答的问题。
    """
    assert approval_audit.digest({"a": 1, "b": 2}) == approval_audit.digest({"b": 2, "a": 1})


def test_digest对内容敏感():
    assert approval_audit.digest({"content": "原文"}) != approval_audit.digest(
        {"content": "改过"}
    )


def test_预览截断且写出原始长度():
    preview = approval_audit.preview({"content": "字" * 600})
    assert len(preview) < 620
    # "原本有多长"本身是审计信息：几千字的写入和几十字的写入是两件不同的事
    assert "字符）" in preview


def test_预览不截断短参数():
    assert approval_audit.preview({"a": 1}) == '{"a": 1}'


def test_digest不因不可序列化而抛():
    class Opaque:
        pass

    # 摘要失败不该把主流程带崩——它是记录，不是闸门
    assert len(approval_audit.digest({"x": Opaque()})) == 64
    assert approval_audit.preview({"x": Opaque()})
