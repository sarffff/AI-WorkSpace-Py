"""LLM-as-judge：评答案质量。

两个裁判，评的是两条不同的链路：

- ``AnswerJudge`` 评单轮 RAG 问答——依据是**检索到的内容**。
- ``TaskJudge`` 评多轮 Agent 任务——依据是**工具实际返回的内容**。
  对 Agent 来说「有没有瞎编」只能拿工具轨迹去对，拿检索结果对是不够的：
  它可能查了网页、读了附件、算了一笔账，这些都不在检索结果里。

``AnswerJudge`` 的两个维度刻意分开打分，因为它们会独立失效：

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

# 裁判的输出预算。推理型模型(glm-4.5 系列等)的思考 token 与可见输出共享
# max_tokens,512 会被思考吃掉大半,可见 JSON 刚开头就截断——本轮全量评估
# 因此 96% 的裁判调用解析失败。1500 给思考留出余量,同时下面的字段级
# 兜底解析保证即使再截断也能把已经吐出来的分数捡回来。
_JUDGE_MAX_TOKENS = 1500


def _score_from_fragment(text: str, field: str) -> float | None:
    """从被截断的输出里抢救单个数值字段。

    JSON 截断通常发生在 reason(最后一个字段)上,而分数字段排在前边——
    整体 loads 失败不代表分数没给。宁可少一个 reason,不要整题作废。
    """
    match = re.search(rf'"{field}"\s*:\s*([0-9]+(?:\.[0-9]+)?)', text)
    if not match:
        return None
    return _clamp_score(match.group(1))


def _bool_from_fragment(text: str, field: str) -> bool | None:
    match = re.search(rf'"{field}"\s*:\s*(true|false)', text)
    if match is None:
        return None
    return match.group(1) == "true"

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

_TASK_RUBRIC = """你是严格的评审，正在评估一个能调用工具的 AI 助手。

「工具证据」是助手这次实际调用工具后拿到的全部内容。「评分标准」写明了本任务
要求助手做到什么。请只依据这两者判断，不要用你自己的知识补充事实。

按 1-5 打分：
- success：是否达成了评分标准描述的目标。5=完全达成，3=部分达成或有明显遗漏，1=没做到。
- grounded：回答里的事实是否都能在工具证据中找到依据。凭空出现的数字、日期、
  流程一律扣分；工具证据为空却给出了具体事实，grounded 必须为 1。

另外单独判断：
- fabricated_tool_output：助手是否声称调用了工具或拿到了资料，但工具证据里没有对应记录。

只输出 JSON，不要任何解释文字：
{"success": <1-5>, "grounded": <1-5>, "fabricated_tool_output": true/false, "reason": "一句话"}"""


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
        # 默认用独立的裁判模型:裁判与被评模型同源会有系统性的自我偏好
        self._model = model or settings.judge_model

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
                max_tokens=_JUDGE_MAX_TOKENS,
                purpose="judge",
            )
        except Exception as exc:
            logger.warning("judge call failed: %s", type(exc).__name__)
            return JudgeVerdict(reason=f"裁判调用失败: {type(exc).__name__}", failed=True)

        raw = completion.content or ""
        payload = _parse_json_object(raw)
        if not payload:
            # 整体 JSON 解析失败(通常是截断),尝试按字段抢救
            reason = "裁判输出截断，按字段抢救"
            if not answerable:
                abstained = _bool_from_fragment(raw, "abstained")
                if abstained is not None:
                    return JudgeVerdict(abstained=abstained, reason=reason)
            else:
                faithfulness = _score_from_fragment(raw, "faithfulness")
                relevance = _score_from_fragment(raw, "relevance")
                if faithfulness is not None or relevance is not None:
                    return JudgeVerdict(
                        faithfulness=faithfulness,
                        relevance=relevance,
                        reason=reason,
                    )
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


@dataclass(slots=True)
class TaskVerdict:
    success: float | None = None
    grounded: float | None = None
    # 声称调了工具/拿到了资料，但轨迹里没有对应记录。这是 Agent 特有的失效模式：
    # 答案本身看不出问题，只有对着轨迹才能发现"它说自己查过"是编的。
    fabricated_tool_output: bool | None = None
    reason: str = ""
    failed: bool = False


# 工具证据给裁判之前先截断。裁判不需要看全文——它判的是"回答里的事实在不在证据里"，
# 而 4000 字符已经覆盖了绝大多数任务的全部工具输出；不设上限则一个大附件任务
# 就能把裁判的输入撑到比被评估的那次回答还贵。
_EVIDENCE_MAX_CHARS = 4000


class TaskJudge:
    """评一次多轮 Agent 任务的最终回答。

    与 ``AnswerJudge`` 的区别只在依据从哪来：这里传入的是工具轨迹，而不是检索
    上下文。评分标准（rubric）逐条写在数据集里，因为"什么算做到了"是任务相关的，
    没法用一句通用标准覆盖——"算对这笔钱"和"如实说明知识库里没有"是两回事。
    """

    def __init__(self, model_adapter, model: str | None = None) -> None:
        self._adapter = model_adapter
        self._model = model or settings.judge_model

    async def judge(
        self, *, question: str, answer: str, evidence: str, rubric: str
    ) -> TaskVerdict:
        if not answer.strip():
            return TaskVerdict(reason="空回答", failed=True)

        clipped = evidence[:_EVIDENCE_MAX_CHARS]
        if len(evidence) > _EVIDENCE_MAX_CHARS:
            clipped += f"\n…（证据已截断，原长 {len(evidence)} 字符）"

        sections = [
            _TASK_RUBRIC,
            f"[评分标准]\n{rubric or '回答应当正确、有据、直接回应问题。'}",
            f"[用户问题]\n{question}",
            f"[工具证据]\n{clipped or '（本次没有任何工具执行记录）'}",
            f"[助手回答]\n{answer}",
        ]

        try:
            completion = await self._adapter.complete(
                messages=[{"role": "user", "content": "\n\n".join(sections)}],
                tools=[],
                model=self._model,
                temperature=0.0,
                max_tokens=_JUDGE_MAX_TOKENS,
                purpose="judge_agent_task",
            )
        except Exception as exc:
            logger.warning("task judge call failed: %s", type(exc).__name__)
            return TaskVerdict(reason=f"裁判调用失败: {type(exc).__name__}", failed=True)

        raw = completion.content or ""
        payload = _parse_json_object(raw)
        if not payload:
            success = _score_from_fragment(raw, "success")
            grounded = _score_from_fragment(raw, "grounded")
            if success is not None or grounded is not None:
                return TaskVerdict(
                    success=success,
                    grounded=grounded,
                    reason="裁判输出截断，按字段抢救",
                )
            return TaskVerdict(reason="裁判输出无法解析", failed=True)

        success = _clamp_score(payload.get("success"))
        grounded = _clamp_score(payload.get("grounded"))
        fabricated = payload.get("fabricated_tool_output")
        return TaskVerdict(
            success=success,
            grounded=grounded,
            fabricated_tool_output=bool(fabricated)
            if isinstance(fabricated, bool)
            else None,
            reason=str(payload.get("reason") or "")[:300],
            failed=success is None and grounded is None,
        )

