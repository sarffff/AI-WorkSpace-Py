"""消息反馈的行为测试。

值得钉住的不是"能不能写进去"，而是几条会真实影响评估质量的规则：
- 一条消息只留一份反馈（改主意是更新，不是再加一个负样本）
- 只能给别人看不到的自己的消息打分（越权直接当"找不到"）
- 内容改过要重新导出，否则评估集里留的是旧标注
"""
from __future__ import annotations

from conftest import run
from services.feedback_service import feedback_service


def test_submit_creates_feedback(db_real, chat_with_answer):
    chat_id, message_id = chat_with_answer

    feedback = feedback_service.submit(
        db_real, "u1", message_id, "down", reason="inaccurate",
        expected_answer="应该是 6 个月",
    )

    assert feedback is not None
    assert feedback.rating == "down"
    assert feedback.expected_answer == "应该是 6 个月"
    # 提问一并存下来，导出回归用例时不必再回表拼
    assert feedback.question == "试用期多久？"


def test_second_submit_updates_instead_of_appending(db_real, chat_with_answer):
    _chat_id, message_id = chat_with_answer

    feedback_service.submit(db_real, "u1", message_id, "down")
    feedback_service.submit(db_real, "u1", message_id, "up")

    summary = feedback_service.summary(db_real, "u1")
    assert summary["rated"] == 1, "反复点击不该被算成多条反馈"
    assert summary["up"] == 1
    assert summary["down"] == 0


def test_editing_feedback_clears_export_mark(db_real, chat_with_answer):
    _chat_id, message_id = chat_with_answer

    first = feedback_service.submit(db_real, "u1", message_id, "down")
    first.exported_at = first.created_at
    db_real.commit()

    updated = feedback_service.submit(
        db_real, "u1", message_id, "down", expected_answer="改过的期望答案"
    )

    assert updated.exported_at is None, "内容改了必须重新导出"


def test_other_users_message_is_not_found(db_real, chat_with_answer):
    _chat_id, message_id = chat_with_answer

    assert feedback_service.submit(db_real, "intruder", message_id, "up") is None


def test_user_message_cannot_be_rated(db_real, chat_with_question):
    _chat_id, message_id = chat_with_question

    assert feedback_service.submit(db_real, "u1", message_id, "up") is None


def test_revoke_removes_feedback(db_real, chat_with_answer):
    _chat_id, message_id = chat_with_answer
    feedback_service.submit(db_real, "u1", message_id, "up")

    assert feedback_service.revoke(db_real, "u1", message_id) is True
    assert feedback_service.summary(db_real, "u1")["rated"] == 0


def test_revoke_of_missing_feedback_is_false(db_real, chat_with_answer):
    _chat_id, message_id = chat_with_answer

    assert feedback_service.revoke(db_real, "u1", message_id) is False


def test_satisfaction_is_none_without_any_feedback(db_real):
    summary = feedback_service.summary(db_real, "u1")

    # 没人评价 ≠ 满意率 0，否则新用户一上来就是"零满意度"
    assert summary["satisfaction"] is None
    assert summary["rated"] == 0


def test_summary_counts_pending_export(db_real, chat_with_answer):
    _chat_id, message_id = chat_with_answer
    feedback_service.submit(db_real, "u1", message_id, "down", reason="off_topic")

    summary = feedback_service.summary(db_real, "u1")

    assert summary["pendingExport"] == 1
    assert summary["downReasons"] == [{"reason": "off_topic", "count": 1}]


def test_for_messages_returns_empty_without_ids(db_real):
    assert feedback_service.for_messages(db_real, "u1", []) == {}
