"""``submit_review``：把审核结论作为结构交出来，而不是写在回答里。

## 为什么是工具，而不是事后从回答文本里抽取

抽取出来的结论只是**回答的摘要**。当回答文本说"这张单子可以通过"而抽取结果是
"需要人判断"时，用户读到的是回答文本——那时结构化那一步不但没用，还制造了一份
看起来权威的假记录。

做成工具，结论本身就是交付物：参数校验在执行之前跑，``ReviewVerdict`` 的
validator（缺料就只能 needs_human）于是变成一道**执行不下去**的门，而不是一句
请求。校验失败的说明会照常回灌给模型，它下一轮可以改。

## 为什么不进审批闸门

闸门问的是"能不能做这个动作"。而这里人要判断的是**结论对不对**，那件事发生在
读台账的时候，不是记账的时候。把 ``submit_review`` 挂进闸门，用户会被问
"允许我记下这条结论吗"——一个他无法据此判断任何东西的问题，点三次之后就变成
无脑点确认，而那会顺带训练他对真正的写文件审批也无脑点。

需要人接手的是 ``needs_human`` 那一档，入口在台账的待办筛选，不在弹窗。

## 与 review_consensus 的关系：已接上，默认不开

``submit_review`` 拿到模型**一次**调用的结论后交给 ``review_consensus.verify_submission``
（见下方落库处）：这份算第一个样本,再独立重采样 ``REVIEW_CONSENSUS_RUNS - 1`` 次逐字段
比对,不一致就把落库结论换成合成的 needs_human。默认 ``=1`` 时它提前返回、不发额外调用,
落库 ``runs=1, agreed=True``——按 ``combine`` 的约定读作"没做一致性检查",不是"检查过且
一致",台账上这两者必须分得开。要真开复审,把它调到 ≥2。
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from config import settings
from services import review_consensus, review_service, skill_service
from services.model_adapter import OpenAICompatibleAdapter
from services.review_consensus import ConsensusResult
from services.structured import ReviewVerdict
from services.tool_runtime import ToolDefinition

logger = logging.getLogger("review_tools")


def _build_submit_tool(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    chat_id: str | None,
    message_id: str | None,
    adapter: Any = None,
) -> ToolDefinition:
    """``adapter`` 只给独立复审用。

    缺省时自己建一个 ``OpenAICompatibleAdapter``——这个工具是在循环内部被调用的，
    而循环手上那个适配器没有沿着工具构建链传下来。让调用方传是为了测试能给替身：
    复审要真的发一次模型调用，不给替身的话这条链只能靠跑真模型来测。
    """
    async def submit_review(arguments: dict[str, Any]) -> str:
        subject = arguments.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            return "提交失败：subject 必须是非空字符串——台账上要能看出审的是什么。"

        sop_name = arguments.get("sop_name")
        if not isinstance(sop_name, str) or not sop_name.strip():
            return "提交失败：sop_name 必须说明按哪份作业指导审的。"

        # SOP 必须真的存在，而且版本号由**服务端**填，不接受模型给的值。
        #
        # 让模型填版本号的话它会从上下文里抄一个看起来合理的数字，而这一列的
        # 全部意义在于事后能对上"当时按的哪一版"——一个抄错的版本号比没有版本号
        # 更糟：它看起来是可追溯的。
        skills = skill_service.available(db, workspace_id)
        skill = skills.get(sop_name.strip())
        if skill is None:
            available = "、".join(sorted(skills)) or "（一个都没有）"
            return (
                f"提交失败：没有叫 {sop_name!r} 的作业指导。"
                f"当前可用：{available}。请先用 load_skill 加载再按它审。"
            )
        version = skill_service.version_of(db, workspace_id, skill)

        raw_inputs = arguments.get("inputs")
        if not isinstance(raw_inputs, list):
            return "提交失败：inputs 必须是数组，逐项核对这份指导要求的材料。"

        # 项数必须和 SOP 声明的对上。少一项是"没检查"，而它和"检查了没找到"
        # 处置相反——前者要它回去看，后者才是转人工。
        #
        # 这一条在 ReviewVerdict 里做不了：那个类拿不到 skill。
        expected = set(skill.required_inputs)
        if expected:
            got = {
                str(item.get("name", "")).strip()
                for item in raw_inputs
                if isinstance(item, dict)
            }
            missing = expected - got
            if missing:
                return (
                    f"提交失败：{sop_name} 要求先核对这些材料，而 inputs 里没有它们："
                    f"{'、'.join(sorted(missing))}。"
                    "每一项都要给出 found（找到了没有），拿不准填 false。"
                )
            extra = got - expected
            if extra:
                # 多出来的项不拦，只提醒：SOP 之外的核对是有价值的（"发票号重复"），
                # 但要让模型知道它不在必备清单里，免得它以为那一项也是必需的
                logger.info(
                    "submit_review for %s carried extra inputs: %s",
                    sop_name,
                    sorted(extra),
                )

        # 依据原文。要求它交上来有两个用途：人复核时要看的就是这个，
        # 独立复审拿它重新判一次。
        #
        # 必填而不是可选：一条没有材料的结论**没法被独立检查**，而那正好是这套
        # 东西存在的理由。允许它为空的话，模型会在拿不准时省掉这个参数，
        # 于是恰恰是最该复审的那些结论没有复审。
        evidence = arguments.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            return (
                "提交失败：evidence 必须是你据以判断的材料原文。"
                "把相关的那几段原文抄进来——台账上要留下依据，"
                "复审也要拿它重新核对一次。"
            )
        limit = max(500, settings.REVIEW_EVIDENCE_MAX_CHARS)
        if len(evidence) > limit:
            # 明确报错而不是静默截断：截掉的正好是尾部，而尾部常常是签字与日期
            return (
                f"提交失败：evidence 有 {len(evidence)} 字符，超过 {limit} 上限。"
                "只抄和这次判断直接相关的那几段，不要把整个文件放进来。"
            )

        try:
            verdict = ReviewVerdict(
                inputs=raw_inputs,
                basis=arguments.get("basis") or [],
                verdict=arguments.get("verdict"),
                sop_name=sop_name.strip(),
                sop_version=version,
            )
        except ValidationError as exc:
            # 原文回灌：它已经点明了是哪个字段、为什么不合法（缺料却给了 pass
            # 这一条就是 validator 抛的）。重写一遍只会更模糊。
            return f"提交失败：{exc.errors()[0].get('msg', exc)}"

        # 独立复审。拿同一份材料重新判 N-1 次，和提交的这份比对；不一致就转人工。
        #
        # 失败不让提交失败（见 verify_submission 的文档串）：这次审核本身是成功的，
        # 复审只是加固，让加固的故障吃掉一条有效结论是把可观测性变成单点故障。
        try:
            result = await review_consensus.verify_submission(
                adapter or OpenAICompatibleAdapter(),
                submitted=verdict,
                instructions=skill.instructions,
                materials=evidence,
                required_inputs=skill.required_inputs,
            )
        except Exception:
            logger.exception("review re-sampling failed; recording unchecked")
            result = ConsensusResult(verdict=verdict, runs=1, agreed=True)

        try:
            row = review_service.record(
                db,
                workspace_id=workspace_id,
                user_id=user_id,
                subject=subject,
                result=result,
                evidence=evidence,
                chat_id=chat_id,
                message_id=message_id,
            )
        except review_service.ReviewError as exc:
            return f"提交失败：{exc}"
        except Exception:
            # 落库失败必须让工具失败（与 approval_audit 相反，理由见
            # review_service 的模块文档）：结论就是交付物，没记上就等于没产出。
            # 说"记下了"会让模型向用户复述"已归档"。
            logger.exception("failed to record review verdict")
            return (
                "提交失败：结论没能写进台账。请把结论直接告诉用户，"
                "并说明这次没有归档成功。"
            )

        # 落库的是**合并之后**的结论，不是模型提交的那份——复审不一致时
        # result.verdict 已经被换成合成的 needs_human。回灌给模型的话也必须以
        # 它为准，否则模型会照自己原来那份向用户复述"通过"，而台账上记的是转人工。
        final = result.verdict
        missing_now = [item.name for item in final.inputs if not item.found]
        if not result.agreed:
            # 分歧要说清楚，而且要让模型知道**它自己那份没有被采纳**
            return (
                f"已记入台账（{row.id[:8]}），但结论被改成了**需要人判断**："
                f"{'；'.join(result.reasons)}。"
                f"你提交的是 {verdict.verdict}，独立复审给出了不同结果，"
                "所以这一条要交给人。请向用户说明分歧在哪，不要坚持你原来的结论。"
            )
        if final.verdict == "needs_human":
            tail = f"缺的材料：{'、'.join(missing_now)}。" if missing_now else ""
            return (
                f"已记入台账（{row.id[:8]}），结论是**需要人判断**。{tail}"
                "请向用户说明缺什么、以及需要谁来定，不要替他做判断。"
            )
        checked = (
            f"（复审 {result.runs} 次一致）"
            if result.runs > 1
            else "（未做独立复审）"
        )
        return (
            f"已记入台账（{row.id[:8]}），结论是 {final.verdict}，"
            f"按 {final.sop_name} 第 {version} 版{checked}。"
            "向用户复述时请带上依据。"
        )

    return ToolDefinition(
        name="submit_review",
        description=(
            "把一次审核的结论记入台账。审完一份材料后调用它——"
            "结论要作为结构提交，不是只写在回答里。"
            "任何一项必备材料没找到时，verdict 只能是 needs_human。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "审的是什么（文件名、单号或一句描述）",
                },
                "sop_name": {
                    "type": "string",
                    "description": "按哪份作业指导审的，用 load_skill 里的名字",
                },
                "inputs": {
                    "type": "array",
                    "description": (
                        "逐项核对这份指导要求的材料，一项都不能少。"
                        "拿不准就把 found 填 false"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value": {
                                "type": "string",
                                "description": "找到的原值，没找到留空",
                            },
                            "found": {"type": "boolean"},
                        },
                        "required": ["name", "found"],
                    },
                },
                "verdict": {
                    "type": "string",
                    "enum": ["pass", "reject", "needs_human"],
                    "description": (
                        "通过 / 不通过 / 需要人判断。"
                        "有任何一项 found=false 时只能是 needs_human"
                    ),
                },
                "basis": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "依据：引用的规程条目 + 材料出处",
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "你据以判断的材料原文，抄相关的那几段。"
                        "台账要留它作依据，复审也要拿它重新核对"
                    ),
                },
            },
            "required": [
                "subject",
                "sop_name",
                "inputs",
                "verdict",
                "evidence",
            ],
            "additionalProperties": False,
        },
        handler=submit_review,
    )


def enabled() -> bool:
    return settings.REVIEW_LEDGER_ENABLED


def build(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    chat_id: str | None = None,
    message_id: str | None = None,
    adapter: Any = None,
) -> list[ToolDefinition]:
    """按开关与"这个工作区有没有审核型 SOP"组装。

    **没有任何声明了必备材料的 SOP 时不注册。** 一个只放写作指导的部署给模型
    ``submit_review`` 只会让它去猜该记什么——同 ``skill_tools.build`` 里
    "一份 skill 都没有就不注册"那条理由。

    判据用 ``required_inputs`` 非空而不是"有没有 skill"：审核型和写作型的区别
    正好落在这个字段上，而它已经是必填结构的来源了。
    """
    if not enabled():
        return []
    skills = skill_service.available(db, workspace_id)
    if not any(skill.required_inputs for skill in skills.values()):
        return []
    return [
        _build_submit_tool(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            adapter=adapter,
        )
    ]


__all__ = ["build", "enabled"]
