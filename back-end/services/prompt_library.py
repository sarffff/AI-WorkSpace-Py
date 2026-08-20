"""提示词注册表：把提示词当成可版本化、可对比的工件，而不是散在代码里的字符串。

为什么要单独抽一层：

1. **提示词是改动最频繁的那部分「代码」，却最不容易 review。**
   写在 Python 里的多行字符串，diff 出来是一坨缩进变化；放进
   ``prompts/<key>/<version>.md`` 之后，「这一版比上一版多了哪句约束」
   在 git 里一眼就能看见。

2. **改提示词必须能回退，也必须能对比。**
   旧版本不删、标成 ``archived`` 留着当对照组，才能回答
   「加那段定界符声明到底值多少分」——这个问题只有 A/B 能答，
   而 A/B 的前提是两个版本同时存在、可被同一套评估跑到。

3. **占位符打错字不该在模型那边才暴露。**
   ``{contxt}`` 这种拼写错误，如果只做 ``str.format``，结果是模型收到
   一段字面量 ``{contxt}`` 然后照样给你一个看着挺像的答案——最难查的那类 bug。
   这里在加载时就把每个模板的占位符和 ``SPECS`` 里声明的集合做严格比对，
   多一个少一个都直接报错。

设计取舍：

- **版本是代码，不是数据。** 模板文件跟着仓库走、进 code review；
  数据库里的 ``prompts`` 表存的是用户自己攒的提示词片段，两回事。
  评估要可复现，就不能让「跑出的分数」取决于某台机器数据库里的某一行。
- **只支持一层 ``[[if flag]]`` 条件段，不支持嵌套、循环、表达式。**
  提示词模板一旦拥有图灵完备的模板语言，逻辑就会开始往模板里搬，
  最后没人说得清模型实际收到了什么。需要更复杂的分支，就再开一个版本。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from config import settings

logger = logging.getLogger("services.prompt_library")

PROMPT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts"
)

# active: 当前默认；candidate: 已就绪、等着被 A/B；archived: 只作对照，不要启用
STATUSES = ("active", "candidate", "archived")

# 模板可以在 frontmatter 里用 ``expects:`` 声明它是为哪种运行时配置写的，
# 逗号分隔。启动时据此校验「配置和提示词是否匹配」——此前这件事只写在 notes
# 里（"开启委派时必须切到这一版"），而注释拦不住任何人。
#
# 为什么是模板自己声明，而不是在 main.py 里列一张版本名表：加新版本时那张表
# 一定会漏掉，而漏掉的表现是"配置错了但程序照跑"，正是这个机制要消灭的东西。
EXPECTATIONS = (
    # 讲了 workspace 那几个工具（calculate / web_search / read_attachment /
    # save_to_knowledge_base）的策略，不只是让模型从 schema 里看到名字
    "workspace-tools",
    # 讲了 delegate 怎么用：任务描述必须自包含、什么时候不该委派
    "delegation",
    # 为 supervisor 模式写的：主代理的专用工具已被角色收走，只能委派
    "supervisor",
)


class PromptError(RuntimeError):
    """模板缺失、占位符不匹配、条件段没闭合——一律在加载或渲染时立刻抛出。"""


@dataclass(frozen=True, slots=True)
class PromptSpec:
    """一类提示词的契约：允许哪些占位符、哪些条件开关、默认用哪一版。"""

    key: str
    purpose: str
    default_version: str
    # 允许用哪个 settings 字段切换版本。为 None 表示这类提示词不做 A/B
    # （比如关掉 RAG 时那句话，短到没有可对比的空间）。
    setting: str | None = None
    required: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    # 是否允许单次请求指定版本。只有对话系统提示词开放:它是用户能在界面上
    # 立刻看到效果的那一个。评估用的提示词不开放——那边要的是"整轮跑下来
    # 配置完全由代码决定",按请求覆盖会让报告里的数字失去可复现性。
    request_overridable: bool = False


SPECS: dict[str, PromptSpec] = {
    "chat_system_plain": PromptSpec(
        key="chat_system_plain",
        purpose="关闭知识库时的对话系统提示词",
        default_version="v1",
    ),
    "chat_system_rag": PromptSpec(
        key="chat_system_rag",
        purpose="开启知识库时的对话系统提示词（工具说明 + 注入防线）",
        # 为什么默认仍是 v2 而不是更全的 v4-workspace：产品默认把四个 workspace
        # 工具全部关掉（见 config.py 那段注释），此时 v4 多出来的三段策略讲的是
        # 网页可信度分层、写操作确认、图片直接可见——全是当下不存在的工具，而它
        # 的固定成本每轮都要付。
        #
        # 所以「v2 还是 v4」不是一个能单独回答的问题，它取决于开了哪些工具：
        # 工具全关时 v2 正确，工具打开时应当跟着切到 v4（main.py 会就此给出警告）。
        # eval 的 prompt-v2 变体量的是后一种情形——它的 _BASE 把四个工具全打开了,
        # 所以那组数字回答的是"开了工具之后 v4 值不值这笔 token",
        # 不是"产品默认该用哪一版"。
        default_version="v2",
        setting="PROMPT_CHAT_SYSTEM_VERSION",
        flags=("prefetched",),
        request_overridable=True,
    ),
    "eval_rag_answer": PromptSpec(
        key="eval_rag_answer",
        purpose="离线评估里生成回答用的单轮提示词",
        default_version="v1",
        setting="PROMPT_EVAL_ANSWER_VERSION",
        required=("context", "question"),
    ),
    "rag_query_condense": PromptSpec(
        key="rag_query_condense",
        purpose="预检索前把追问改写成自包含问题（指代消解）",
        default_version="v1",
        required=("recent_turns", "question"),
    ),
    # 这两个原来是源码里的多行字符串。搬出来的理由和其它提示词一样：它们同样
    # 发给模型、同样影响结果、同样该被 review 和 A/B。记忆抽取尤其重要——
    # 它的排除段是注入防线的一部分（见 memory_service 模块文档）。
    "history_summary": PromptSpec(
        key="history_summary",
        purpose="把滑出 token 预算的早期对话压成滚动摘要",
        default_version="v1",
        required=("previous", "transcript"),
        flags=("has_previous",),
    ),
    "memory_extract": PromptSpec(
        key="memory_extract",
        purpose="从一轮对话里抽取值得跨会话记住的用户事实与偏好",
        default_version="v1",
        required=("question", "answer"),
    ),
    # 子代理各自一个 key,而不是共用一个带 [[if role]] 分支的模板:三个角色的
    # 约束几乎不重叠(researcher 要讲出处分层,analyst 要讲"缺输入就停",
    # critic 要讲"没依据别提"),塞进一版会变成一个谁都不好改的大文件,
    # 而且改 researcher 那段会让 critic 的评估结果一起失效。
    # 三个角色各自一个 setting，而不是共享一个 PROMPT_AGENT_VERSION：理由和
    # 上面"为什么是三个 key 而不是一个带 [[if role]] 分支的模板"一样——共享的话
    # 没法单独 A/B researcher，动一个会让另两个的结果一起失效。
    #
    # 不开 request_overridable：子代理的版本由谁定应当和主代理解耦，而单次请求
    # 只带一个 prompt_version 字段，语义上指的是对话系统提示词。要按请求覆盖
    # 子代理版本得先想清楚那个字段怎么扩展，现在没有这个需求。
    "agent_researcher": PromptSpec(
        key="agent_researcher",
        purpose="researcher 子代理：查资料并如实汇报出处",
        default_version="v1",
        setting="PROMPT_AGENT_RESEARCHER_VERSION",
    ),
    "agent_analyst": PromptSpec(
        key="agent_analyst",
        purpose="analyst 子代理：精确计算与读取附件",
        default_version="v1",
        setting="PROMPT_AGENT_ANALYST_VERSION",
    ),
    "agent_critic": PromptSpec(
        key="agent_critic",
        purpose="critic 子代理：只依据给定材料审查草稿",
        default_version="v1",
        setting="PROMPT_AGENT_CRITIC_VERSION",
    ),
}

_FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
# 同时匹配转义写法：``{{`` / ``}}`` 用来输出字面量花括号，
# 否则模板里放一段 JSON 示例就会被当成占位符。
_TOKEN = re.compile(r"\{\{|\}\}|\{([a-z_][a-z0-9_]*)\}")
_CONDITION = re.compile(r"\[\[(?:if\s+(!?)([a-z_][a-z0-9_]*)|(endif))\]\]")


def _placeholder_names(body: str) -> tuple[str, ...]:
    names: list[str] = []
    for match in _TOKEN.finditer(body):
        name = match.group(1)
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _flag_names(body: str) -> tuple[str, ...]:
    names: list[str] = []
    for match in _CONDITION.finditer(body):
        name = match.group(2)
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _apply_conditions(body: str, flags: dict[str, bool], where: str) -> str:
    """按行处理 ``[[if flag]] ... [[endif]]``。标记行本身永远不出现在输出里。"""
    kept: list[str] = []
    active: bool | None = None  # None 表示当前不在任何条件段内
    for line in body.splitlines():
        match = _CONDITION.fullmatch(line.strip())
        if match:
            if match.group(3):  # endif
                if active is None:
                    raise PromptError(f"{where}: [[endif]] 没有对应的 [[if]]")
                active = None
            else:
                if active is not None:
                    raise PromptError(f"{where}: 条件段不支持嵌套")
                value = bool(flags.get(match.group(2)))
                active = (not value) if match.group(1) == "!" else value
            continue
        if active is None or active:
            kept.append(line)
    if active is not None:
        raise PromptError(f"{where}: [[if]] 没有闭合")
    return "\n".join(kept)


def _substitute(body: str, values: dict[str, str], where: str) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        literal = match.group(0)
        if literal == "{{":
            return "{"
        if literal == "}}":
            return "}"
        name = match.group(1)
        if name not in values:
            missing.append(name)
            return literal
        return values[name]

    rendered = _TOKEN.sub(replace, body)
    if missing:
        raise PromptError(f"{where}: 缺少占位符取值 {sorted(set(missing))}")
    return rendered


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    key: str
    version: str
    label: str
    status: str
    notes: str
    body: str
    placeholders: tuple[str, ...]
    flags: tuple[str, ...]
    # frontmatter 的 ``expects:``。这一版是为哪种运行时配置写的，供启动校验用。
    expects: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        """写进埋点的版本标识。一条 trace 必须能回答「这是哪一版提示词跑出来的」。"""
        return f"{self.key}@{self.version}"

    def expects_all(self, *names: str) -> bool:
        return all(name in self.expects for name in names)

    def render(self, *, flags: dict[str, bool] | None = None, **values: Any) -> str:
        where = f"prompts/{self.key}/{self.version}.md"
        declared = set(self.flags)
        unknown = sorted(set(flags or {}) - declared)
        if unknown:
            # 传了模板里不存在的开关，通常意味着换版本时漏改了调用方：
            # 静默忽略的话，行为差异要到线上才被发现。
            raise PromptError(f"{where}: 模板没有声明条件开关 {unknown}")
        body = _apply_conditions(self.body, flags or {}, where)
        # 即使没有占位符也要走一遍替换：模板里的 ``{{`` / ``}}`` 转义需要在这里
        # 还原成字面量花括号（比如提示词里带一段 JSON 示例）。
        return _substitute(
            body, {key: str(value) for key, value in values.items()}, where
        ).strip()


def _parse(raw: str, key: str, version: str) -> PromptTemplate:
    where = f"prompts/{key}/{version}.md"
    match = _FRONTMATTER.match(raw)
    if not match:
        raise PromptError(f"{where}: 缺少 --- 包裹的元数据块")

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise PromptError(f"{where}: 元数据行缺少冒号：{line!r}")
        meta[name.strip()] = value.strip()

    body = raw[match.end() :].strip()
    if not body:
        raise PromptError(f"{where}: 正文为空")

    status = meta.get("status", "candidate")
    if status not in STATUSES:
        raise PromptError(f"{where}: status 只能是 {STATUSES} 之一，收到 {status!r}")

    # 拼错的 expects 必须在这里炸：静默忽略的话，启动校验会以为这一版"没有声明
    # 任何前置条件"从而放行，而那正是错配能溜过去的方式。
    expects = tuple(
        item.strip() for item in meta.get("expects", "").split(",") if item.strip()
    )
    unknown_expects = sorted(set(expects) - set(EXPECTATIONS))
    if unknown_expects:
        raise PromptError(
            f"{where}: 未知的 expects 取值 {unknown_expects}，可用 {list(EXPECTATIONS)}"
        )

    spec = SPECS[key]
    placeholders = _placeholder_names(body)
    flags = _flag_names(body)
    if set(placeholders) != set(spec.required):
        # 严格相等而不是"包含"：多一个占位符是拼写错误，少一个是漏了必要输入，
        # 两种都会让模型收到一段自洽但错误的提示词，比直接报错难查得多。
        raise PromptError(
            f"{where}: 占位符与契约不一致，模板 {sorted(placeholders)} "
            f"vs 声明 {sorted(spec.required)}"
        )
    unknown_flags = sorted(set(flags) - set(spec.flags))
    if unknown_flags:
        raise PromptError(f"{where}: 未声明的条件开关 {unknown_flags}")

    # 校验条件段结构：两种取值各跑一遍，才能同时覆盖 if 与 if! 分支
    for probe in ({name: True for name in flags}, {name: False for name in flags}):
        _apply_conditions(body, probe, where)

    return PromptTemplate(
        key=key,
        version=version,
        label=meta.get("label") or version,
        status=status,
        notes=meta.get("notes", ""),
        body=body,
        placeholders=placeholders,
        flags=flags,
        expects=expects,
    )


_registry: dict[str, dict[str, PromptTemplate]] | None = None


def _load() -> dict[str, dict[str, PromptTemplate]]:
    registry: dict[str, dict[str, PromptTemplate]] = {}
    for key in SPECS:
        directory = os.path.join(PROMPT_DIR, key)
        if not os.path.isdir(directory):
            raise PromptError(f"提示词目录不存在：prompts/{key}")
        versions: dict[str, PromptTemplate] = {}
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".md"):
                continue
            version = filename[: -len(".md")]
            with open(os.path.join(directory, filename), "r", encoding="utf-8") as handle:
                versions[version] = _parse(handle.read(), key, version)
        if not versions:
            raise PromptError(f"提示词目录里没有任何版本：prompts/{key}")
        default = SPECS[key].default_version
        if default not in versions:
            raise PromptError(f"prompts/{key}: 默认版本 {default} 不存在")
        if versions[default].status == "archived":
            # 把还在被引用的版本标成 archived，是典型的"以为已经切走了"
            raise PromptError(f"prompts/{key}: 默认版本 {default} 已标记 archived")
        registry[key] = versions
    return registry


def registry() -> dict[str, dict[str, PromptTemplate]]:
    global _registry
    if _registry is None:
        _registry = _load()
    return _registry


def reload() -> dict[str, dict[str, PromptTemplate]]:
    """丢掉缓存重新读盘。只给测试和本地调试用——改提示词不必重启进程。"""
    global _registry
    _registry = None
    return registry()


def validate() -> int:
    """启动时调用：宁可在这里起不来，也不要等第一个用户提问才炸。"""
    loaded = registry()
    total = sum(len(versions) for versions in loaded.values())
    logger.info("prompt library: %s keys / %s versions", len(loaded), total)
    return total


def _spec(key: str) -> PromptSpec:
    spec = SPECS.get(key)
    if spec is None:
        raise PromptError(f"未注册的提示词 key：{key!r}")
    return spec


def resolve_version(key: str, version: str | None = None) -> str:
    """版本解析顺序：显式传入 > settings 配置 > 契约里的默认值。

    显式传入优先，是为了让"同一进程里并发跑两个版本"成为可能——
    如果只能改全局设置，两个用户同时试不同版本就会互相踩。
    """
    spec = _spec(key)
    if version:
        return version
    if spec.setting:
        configured = getattr(settings, spec.setting, None)
        if configured:
            return str(configured)
    return spec.default_version


def get(key: str, version: str | None = None) -> PromptTemplate:
    versions = registry()[_spec(key).key]
    resolved = resolve_version(key, version)
    template = versions.get(resolved)
    if template is None:
        raise PromptError(
            f"prompts/{key}: 没有版本 {resolved}，现有 {sorted(versions)}"
        )
    return template


def versions(key: str) -> list[PromptTemplate]:
    return [template for _, template in sorted(registry()[_spec(key).key].items())]


def render(key: str, *, version: str | None = None, **kwargs: Any) -> str:
    return get(key, version).render(**kwargs)


def available_request_versions() -> list[str]:
    """允许由单次请求指定的版本集合。

    校验放在这里而不是路由里写死一个 key:哪类提示词可以按请求覆盖是
    ``SPECS`` 说的算,两处各写一遍迟早会对不上。
    """
    found: set[str] = set()
    for key, spec in SPECS.items():
        if not spec.request_overridable:
            continue
        found.update(template.version for template in versions(key))
    return sorted(found)


def catalog() -> list[dict[str, Any]]:
    """给前端用的只读目录。正文一起给出——提示词实验台的重点就是看正文差异。"""
    entries: list[dict[str, Any]] = []
    for key, spec in SPECS.items():
        active = resolve_version(key)
        entries.append(
            {
                "key": key,
                "purpose": spec.purpose,
                "activeVersion": active,
                "switchable": bool(spec.setting),
                "requestOverridable": spec.request_overridable,
                "setting": spec.setting,
                "placeholders": list(spec.required),
                "flags": list(spec.flags),
                "versions": [
                    {
                        "version": template.version,
                        "label": template.label,
                        "status": template.status,
                        "notes": template.notes,
                        "body": template.body,
                        "chars": len(template.body),
                        "isActive": template.version == active,
                        # 提示词实验台上要能看出"这一版需要什么配置才成立"
                        "expects": list(template.expects),
                    }
                    for template in versions(key)
                ],
            }
        )
    return entries
