"""线上抽样送评测试：问答配对、低分判定、报告聚合、导出去重、judge 编排（替身）。

配对与导出用 db_real / 临时文件直接验；judge_turns 注入假 knowledge + 假 judge，
不打真实模型。
"""
from __future__ import annotations

from eval import online_sample
from eval.judge import JudgeVerdict
from eval.online_sample import Scored, Turn
from models import Chat, Message, User
from services.clock import naive_now
from conftest import run


def _seed_turn(db, *, uid, ws, chat_id, q, a, base_seq):
    if db.get(User, uid) is None:
        db.add(User(id=uid, email=f"{uid}@x.com", workspace_id=ws))
    if db.get(Chat, chat_id) is None:
        db.add(Chat(id=chat_id, title="t", user_id=uid))
    now = naive_now()
    db.add(Message(id=f"{chat_id}-u{base_seq}", content=q, role="user",
                   chat_id=chat_id, seq=base_seq, created_at=now))
    db.add(Message(id=f"{chat_id}-a{base_seq}", content=a, role="assistant",
                   chat_id=chat_id, seq=base_seq + 1, created_at=now))


class FakeKnowledge:
    def __init__(self, context="参考内容"):
        self.context = context
        self.calls = []

    async def build_rag_context_with_citations(self, session, query, workspace_id, top_k=5, viewer_id=None):
        self.calls.append((query, workspace_id, viewer_id))
        return self.context, []


class FakeJudge:
    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.calls = []

    async def judge(self, *, question, answer, context, answerable):
        self.calls.append({"question": question, "context": context, "answerable": answerable})
        item = self._verdicts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ========== recent_turns：配对 ==========


def test_recent_turns_pairs_question_and_answer(db_real):
    _seed_turn(db_real, uid="u1", ws="w1", chat_id="c1", q="赔付上限多少", a="是 500 元", base_seq=1)
    db_real.commit()
    turns = online_sample.recent_turns(db_real, limit=10)
    assert len(turns) == 1
    assert turns[0].question == "赔付上限多少" and turns[0].answer == "是 500 元"
    assert turns[0].user_id == "u1" and turns[0].workspace_id == "w1"
    assert turns[0].message_id == "c1-a1"


def test_recent_turns_skips_assistant_without_question(db_real):
    """没有前导 user 消息的 assistant（系统首条/脏数据）跳过，不空配。"""
    db_real.add(User(id="u1", email="u1@x.com", workspace_id="w1"))
    db_real.add(Chat(id="c1", title="t", user_id="u1"))
    db_real.add(Message(id="c1-a1", content="孤立回答", role="assistant",
                        chat_id="c1", seq=1, created_at=naive_now()))
    db_real.commit()
    assert online_sample.recent_turns(db_real, limit=10) == []


def test_recent_turns_limit_and_recency(db_real):
    """按 seq 倒序取最近 N 条。"""
    for i in range(5):
        _seed_turn(db_real, uid="u1", ws="w1", chat_id=f"c{i}", q=f"问{i}", a=f"答{i}", base_seq=i * 2 + 1)
    db_real.commit()
    turns = online_sample.recent_turns(db_real, limit=2)
    assert len(turns) == 2
    assert turns[0].answer == "答4"  # 最新的 seq 最大


# ========== _low_score / compute_report ==========


def test_low_score_rules():
    assert online_sample._low_score(Scored(turn=None, relevance=2.0, faithfulness=5.0), 3.0) is True
    assert online_sample._low_score(Scored(turn=None, relevance=4.0, faithfulness=4.0), 3.0) is False
    # 两维都缺（裁判没给分）→ 不算低分，交给 failed 统计
    assert online_sample._low_score(Scored(turn=None), 3.0) is False
    # 裁判失败的样本不算低分（它是"没判成"，不是"判低了"）
    assert online_sample._low_score(Scored(turn=None, relevance=1.0, failed=True), 3.0) is False


def test_compute_report_aggregates():
    scored = [
        Scored(turn=None, faithfulness=5.0, relevance=5.0),
        Scored(turn=None, faithfulness=4.0, relevance=2.0),  # relevance 低
        Scored(turn=None, failed=True, reason="judge_error:X"),
    ]
    report = online_sample.compute_report(scored, threshold=3.0)
    assert report["sampled"] == 3 and report["judged"] == 2 and report["failed"] == 1
    assert report["relevance"]["min"] == 2.0
    assert report["lowScorers"] == 1
    assert report["faithfulness"]["mean"] == 4.5


# ========== judge_turns：编排（替身） ==========


def _turn(ws="w1", q="q1", a="a1"):
    return Turn(question=q, answer=a, chat_id="c1", message_id="m123456", user_id="u1", workspace_id=ws)


def test_judge_turns_maps_verdicts(db_real):
    knowledge = FakeKnowledge(context="参考X")
    judge = FakeJudge([JudgeVerdict(faithfulness=4.0, relevance=5.0, reason="ok")])
    scored = run(online_sample.judge_turns(db_real, [_turn()], knowledge=knowledge, judge=judge))
    assert len(scored) == 1 and scored[0].relevance == 5.0 and scored[0].faithfulness == 4.0
    assert knowledge.calls == [("q1", "w1", "u1")]  # 重检索按 owner 作用域
    assert judge.calls[0]["context"] == "参考X" and judge.calls[0]["answerable"] is True


def test_judge_turns_no_workspace_skips_retrieval(db_real):
    knowledge = FakeKnowledge()
    judge = FakeJudge([JudgeVerdict(faithfulness=3.0, relevance=3.0)])
    scored = run(online_sample.judge_turns(db_real, [_turn(ws=None)], knowledge=knowledge, judge=judge))
    assert knowledge.calls == []  # 无 workspace 不重检索
    assert judge.calls[0]["context"] == ""
    assert scored[0].relevance == 3.0


def test_judge_turns_single_failure_isolated(db_real):
    judge = FakeJudge([RuntimeError("boom"), JudgeVerdict(faithfulness=4.0, relevance=4.0)])
    scored = run(online_sample.judge_turns(
        db_real, [_turn(q="q1"), _turn(q="q2")], knowledge=FakeKnowledge(), judge=judge))
    assert scored[0].failed is True and "judge_error" in scored[0].reason
    assert scored[1].failed is False and scored[1].relevance == 4.0


# ========== export_low_scorers：导出 + 去重 ==========


def test_export_writes_and_dedups(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(online_sample, "OUTPUT_PATH", str(path))
    scored = [Scored(turn=_turn(q="赔付上限", a="不知道"), relevance=2.0, faithfulness=4.0)]
    cases = online_sample.export_low_scorers(scored, threshold=3.0)
    assert len(cases) == 1
    assert cases[0]["needs_review"] is True and cases[0]["probe"] == "online_sample"
    assert cases[0]["reference_answer"] == "" and cases[0]["expected_documents"] == []
    # 重跑按 id 去重，不重复追加
    assert online_sample.export_low_scorers(scored, threshold=3.0) == []
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_export_dry_run_no_write(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(online_sample, "OUTPUT_PATH", str(path))
    cases = online_sample.export_low_scorers([Scored(turn=_turn(), relevance=1.0)], threshold=3.0, dry_run=True)
    assert len(cases) == 1 and not path.exists()


def test_export_skips_high_scorers(tmp_path, monkeypatch):
    path = tmp_path / "out.jsonl"
    monkeypatch.setattr(online_sample, "OUTPUT_PATH", str(path))
    scored = [Scored(turn=_turn(), relevance=5.0, faithfulness=5.0)]
    assert online_sample.export_low_scorers(scored, threshold=3.0) == []
    assert not path.exists()
