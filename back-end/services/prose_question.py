"""认出"模型在回答正文里问了用户一个问题"。

## 为什么需要这个模块

``ask_user`` 那条链（挂起 → 快照 → ``/answer`` → 接着同一轮跑）从做完起就
**没有被走进去过一次**。三次实测（2026-08-30 / 08-31，baseline 与 no-prefetch
两种配置、改过一轮用例设计、补过一版专门讲策略的提示词），
``clarificationAsked`` 始终是 0。模型不是不知道有这个工具：闸门内
``ask_user in enabled_names()`` 实测为 ``True``。它是**认出了缺前提、也把问题
问了出来，但问在正文里**：

    「由于您没有说明出差所在的城市等级，我需要确认一下：您出差的城市属于
      一线城市、二线城市还是其他城市？」

对用户来说这两种看起来一样，代价不一样：

- 调 ``ask_user``：这一轮挂起，``messages`` 连同**完整的工具结果**（每条上限
  ``TOOL_RESULT_MAX_CHARS``，默认 4000 字）落进快照。答案回来接着这一轮跑，
  模型手里还有它刚检索到的一切。
- 在正文里问：这一轮以 ``status=done`` 正常收尾。用户的回答变成新一轮，
  上一轮的工具结果只能靠轨迹回灌带过去——而那是**压成 240 字/步、总预算
  600 token 的摘要**（见 ``tool_history.render_block``）。模型据摘要判断细节
  不够时会重新检索一次。所以代价不是"全部丢掉"（我一开始说错了），而是
  "降级成摘要 + 一次多余的重查 + 一轮往返"。

三次尝试之后的结论：模型手里有个更便宜的策略——前提只有三个离散取值时**直接
枚举**（一线 1200 / 二线 900 / 其他 700），用户不用多跑一轮就能自己对号。那甚至
不算错。所以别再往提示词里加更强的措辞（同一条死路走过两次），改由框架把正文
里那句问题**收编**成一次真的中断。

## 判据为什么这么保守

误判的代价不对称：

- **漏判**（真问题没收编）= 退回今天的行为，用户多打一轮字。可接受。
- **误判**（把客套话收编成中断）= 一个已经答完的回合被挂成 ``waiting_input``，
  用户看到一个莫名其妙的输入框，run 卡在那里等一个不存在的答案。**不可接受。**

所以三个条件全都要满足，缺一不收编。宁可漏。
"""
from __future__ import annotations

import re

# 结尾的客套邀请。这些都带问号，是最主要的误判来源——它们不是"缺前提"，
# 而是"活儿干完了，还有别的事吗"。收编它们就是把一个完整的回答挂起。
_COURTESY = (
    "还需要",
    "还有什么",
    "还有其他",
    "需要我",
    "要我帮",
    "有什么可以",
    "是否需要",
    "如果需要",
    "如有需要",
    "希望对你",
    "希望对您",
    "满意吗",
    "清楚了吗",
    "明白了吗",
    "可以吗",
    "好吗",
    # "如有疑问请告诉我" 这类:带了求告知措辞,但要的不是缺失的前提,
    # 而是"以后有事再说"。放宽求告知判据(不再硬要求问号)之后,这一组是
    # 新增的主要误判来源。
    "如有疑问",
    "有疑问",
    "有问题随时",
    "随时告诉我",
    "随时联系",
    "再告诉我",
)

# 真的在要一个前提。"哪个/哪种/几"这类疑问词 + 指向用户的称谓。
# 只有问号是不够的：反问句（"这不就是重复报销了吗？"）同样带问号。
_ASKING = (
    "请问",
    "请告诉",
    "请提供",
    "请确认",
    "请补充",
    "麻烦告诉",
    "麻烦提供",
    "想确认",
    "需要确认",
    "需要知道",
    "需要您提供",
    "需要你提供",
    "能否告诉",
    "可以告诉",
    "告诉我",
    # "请告知" 是"请告诉"之外的另一种常见写法,实测出现过。同一个言语行为,
    # 漏一个词就整条漏判——这就是为什么词表要靠实测原话来长,不能靠想。
    "请告知",
    "还请告知",
    "麻烦告知",
)

_INTERROGATIVE = ("哪", "什么", "多少", "几", "是否", "还是")

_YOU = ("你", "您")

# 结尾句的切分。中英文句末标点都要认，因为模型输出经常混排。
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")

# 一句话里如果这两个都出现，说明它在把选项摆出来让用户挑——那正是"缺前提"
# 最常见的问法（"是一线城市、二线城市还是其他城市？"）。
_CHOICE = "还是"


def _tail_sentences(text: str, count: int = 2) -> list[str]:
    """取末尾 ``count`` 句非空句子。

    问题几乎总在结尾：模型的写法是先把查到的东西讲清楚，最后一句问缺的那个前提。
    只看最后一句会漏掉"问完又补一句注意事项"的情况，所以取两句。
    """
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    return parts[-count:] if parts else []


def _has_question_mark_near_end(text: str, count: int = 2) -> bool:
    """末尾两句范围内有没有问号。

    不能只看 ``text.rstrip().endswith("？")``：实测有一条是
    「……请问您出差的城市属于哪个等级？这样我就能为您计算出总额。」——
    问句在倒数第二句，最后一句是陈述。
    """
    parts = [part for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    if not parts:
        return False
    # 用原文重建"末尾 count 句所覆盖的那一段",再在那一段里找问号
    tail_parts = parts[-count:]
    anchor = tail_parts[0]
    index = text.rfind(anchor)
    tail = text[index:] if index >= 0 else text
    return "？" in tail or "?" in tail


def looks_like_question_to_user(answer: str) -> bool:
    """这段最终回答是不是"在向用户要一个只有用户才知道的前提"。

    ## 问号是充分条件之一，不是必要条件

    第一版硬要求末尾有问号，2026-08-31 实测整轮漏判：同一个变体、同一版提示词、
    温度 0.0，两次运行的措辞不一样——

        16:11  「请问您出差的城市属于哪个等级？」
        18:53  「请告诉我您出差所在的城市等级，我可以给您更准确的报销金额计算。」

    后者没有问号，但它和前者是**同一个言语行为**：要一个只有用户知道的前提。
    温度 0 也不保证措辞稳定（批量推理里很常见），所以判据不能挂在标点上。

    于是改成两条并列的充分条件，任一成立即可：

    - **求告知祈使句**（"请告诉我X" / "请问X" / "麻烦提供X"）。这一条不看标点：
      它本身就是在要信息。
    - **疑问句形式**：末尾两句里有问号，且（疑问词 + 你/您）或者一个 "A 还是 B"
      的选择问。没有求告知措辞时才需要问号来确认这是个问句——
      "这条制度是 2024 年修订的？" 有问号但不是在要前提。

    否决项照旧优先：客套邀请命中就不收编，不管它多像个问题。放宽求告知判据之后
    这一层更重要了——"如有疑问请告诉我" 同时命中求告知和客套，必须判不收编。
    """
    text = (answer or "").strip()
    if not text:
        return False

    sentences = _tail_sentences(text)
    if not sentences:
        return False

    has_mark = _has_question_mark_near_end(text)

    # **逐句判**,不是把末尾两句拼起来判。
    #
    # 拼起来判的话,客套否决会跨句误杀:实测有一条是
    #   「请确认您出差的城市等级，以便确定具体的报销金额。如果需要进一步协助，
    #     请提供更多细节。」
    # 前一句是真的求告知,后一句是客套。拼成一个串之后 "如果需要" 命中否决项,
    # 整条被判不收编——而"问完又补一句客套"是极常见的写法。
    for sentence in sentences:
        # 这一句本身是客套就跳过它,不影响另一句的判断
        if any(word in sentence for word in _COURTESY):
            continue
        if any(word in sentence for word in _ASKING):
            return True
        if not has_mark:
            continue
        if _CHOICE in sentence and any(
            word in sentence for word in _INTERROGATIVE
        ):
            return True
        if any(word in sentence for word in _YOU) and any(
            word in sentence for word in _INTERROGATIVE
        ):
            return True
    return False


def extract_question(answer: str) -> str:
    """把要问用户的那句话摘出来，给中断请求当 ``question``。

    优先级：末尾两句里**最后一个带问号的句子** → 最后一个**带求告知措辞**的句子
    → 末尾一句。

    中间那一档是跟着 ``looks_like_question_to_user`` 放宽判据一起加的：措辞可能
    压根没有问号（"请告诉我您出差所在的城市等级，我可以给您更准确的计算。"），
    只按问号找会摘到后半句那个陈述分句，卡片标题就变成"我可以给您更准确的计算？"。

    一路都摘不出来就退回末尾一句——调用方已经确认过这里有问题，宁可给一句不那么
    精确的，也不要给空字符串（前端那张卡片靠它显示标题）。
    """
    text = (answer or "").strip()
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    if not parts:
        return ""
    tail_parts = parts[-2:]

    # _SENTENCE_SPLIT 把问号本身吃掉了,所以靠"原文里这一句后面紧跟问号"来判断
    questions = []
    for part in tail_parts:
        index = text.find(part)
        if index < 0:
            continue
        after = text[index + len(part) : index + len(part) + 1]
        if after in ("？", "?"):
            questions.append(part)
    if questions:
        chosen = questions[-1]
        return chosen if chosen.endswith(("？", "?")) else chosen + "？"

    # 客套句要排掉:"请确认X。如果需要请提供更多细节。" 里后一句也命中 _ASKING
    # ("请提供"),不排掉就会把标题摘成那句客套。
    asking = [
        part
        for part in tail_parts
        if any(word in part for word in _ASKING)
        and not any(word in part for word in _COURTESY)
    ]
    chosen = asking[-1] if asking else parts[-1]
    # 求告知句常带一个说明性的后半句("…，我可以给您更准确的计算"),砍掉它:
    # 卡片标题要的是"要问什么",不是"问到之后能干什么"。
    head = chosen.split("，")[0].strip()
    if any(word in head for word in _ASKING):
        chosen = head
    return chosen if chosen.endswith(("？", "?")) else chosen + "？"
