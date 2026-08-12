"""LLM-as-judge：评答案质量。

两个维度刻意分开打分，因为它们会独立失效：

- **faithfulness（忠实度）**：答案有没有超出检索到的内容瞎编。
  这是 RAG 最要防的失效模式——答案读起来很对，但依据是模型自己编的。
- **relevance（相关性）**：答案有没有真正回答问题。
  检索准、也没编，但答非所问，同样是失败。

对「知识库里根本没有」的问题另算一档 **abstention（拒答）**：
正确行为是说明未找到，而不是给一个流畅的错误答案。

裁判本身也会错，所以：温度固定为 0、要求先给依据再给分、
输出严格 JSON。分数只用于变体之间的**相对比较**，不当绝对质量指标。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from config import settings

logger = logging.getLogger("eval.judge")

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_RUBRIC = """你是严格的评审。根据「参考内容」判断「回答」的质量，不要用你自己的知识补充。

按 1-5 打分：
- faithfulness：回答中的事实是否都能在参考内容里找到依据。5=全部有据，3=部分无据，1=主要靠编。
- relevance：回答是否切题、直接回应了问题。5=完全切题，1=答非所问。

只输出 JSON，不要任何解释文字：
{"faithfulness": <1-5>, "relevance": <1-5>, "unsupported_claims": ["..."], "reason": "一句话"}"""

_ABSTENTION_RUBRIC = """你是严格的评审。这个问题在知识库中**没有**答案，正确行为是说明未找到相关内容。

判断「回答」是否正确地拒答了。编造了具体信息（数字、流程、金额）即为失败。

只输出 JSON，不要任何解释文字：
{"abstained": true/false, "fabricated": ["..."], "reason": "一句话"}"""


@dataclass(slots=True)
class JudgeVerdict:
    faithfulness: float | None = None
    relevance: float | None = None
    abstained: bool | None = None
    reason: str = ""
    failed: bool = False


def _parse_json_object(text: str) -> dict:
    """模型常在 JSON 外面裹一层解释或代码围栏，这里只抠出第一个对象。"""
    if not text:
        return {}
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _clamp_score(value: object) -> float | None:
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return min(5.0, max(1.0, score))


class AnswerJudge:
    def __init__(self, model_adapter, model: str | None = None) -> None:
        self._adapter = model_adapter
        self._model = model or settings.LLM_MODEL

    async def judge(
        self, *, question: str, answer: str, context: str, answerable: bool
    ) -> JudgeVerdict:
        if not answer.strip():
            return JudgeVerdict(reason="空回答", failed=True)

        rubric = _RUBRIC if answerable else _ABSTENTION_RUBRIC
        sections = [rubric, f"[问题]\n{question}"]
        if answerable:
            sections.append(f"[参考内容]\n{context or '（检索结果为空）'}")
        sections.append(f"[回答]\n{answer}")

        try:
            completion = await self._adapter.complete(
                messages=[{"role": "user", "content": "\n\n".join(sections)}],
                tools=[],
                model=self._model,
                temperature=0.0,
                max_tokens=512,
                purpose="judge",
            )
        except Exception as exc:
            logger.warning("judge call failed: %s", type(exc).__name__)
            return JudgeVerdict(reason=f"裁判调用失败: {type(exc).__name__}", failed=True)

        payload = _parse_json_object(completion.content)
        if not payload:
            # 解析失败必须标成 failed 而不是当 0 分，否则会污染均值
            return JudgeVerdict(reason="裁判输出无法解析", failed=True)

        reason = str(payload.get("reason") or "")[:300]
        if not answerable:
            abstained = payload.get("abstained")
            return JudgeVerdict(
                abstained=bool(abstained) if isinstance(abstained, bool) else None,
                reason=reason,
                failed=not isinstance(abstained, bool),
            )

        faithfulness = _clamp_score(payload.get("faithfulness"))
        relevance = _clamp_score(payload.get("relevance"))
        return JudgeVerdict(
            faithfulness=faithfulness,
            relevance=relevance,
            reason=reason,
            failed=faithfulness is None and relevance is None,
        )
