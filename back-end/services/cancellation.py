"""进程内的执行取消信号。

用户点"停止生成"要能**真的**停下正在跑的 Agent 循环，而不是只把浏览器那条 SSE
连接掐掉。掐连接只是让服务端在下一个 ``await`` 点收到 ``CancelledError``——它能
停下当前这一步，却分不清"用户不想跑了"和"网络抖了一下"，于是这一行会落到
``interrupted``（可接续），出现在"接着跑吗"的列表里。用户明明点了停止，系统却
问要不要继续，这是错的。

这里补的是一个**显式的停止意图**：一个 run_id 到 ``asyncio.Event`` 的进程内表。
循环在安全点（轮首、每个工具执行之前）查它，命中就把执行收尾成 ``cancelled``
（终态，不可接续）。

## 为什么是进程内，不是数据库

取消请求必须到达**正在跑这个循环的那个进程**。``asyncio.Event`` 天生跨不了进程，
硬要跨就得轮询数据库或上 pub/sub。而本项目当前的部署形态就是单进程——默认向量库
是 ``memory``，多 worker 各建一份索引，所以本来就跑不了多 worker（见
``.env.example`` 里 VECTOR_STORE 那段）。在这个前提下，进程内表是最直接、无轮询
开销、也无隔离级别陷阱的做法。

多 worker 的情形没有忽略，只是分工不同：``signal()`` 找不到活着的循环时返回
``False``，路由那边据此**退回落库**（把 ``running``/``interrupted`` 直接标成
``cancelled``），至少让 UI 与"可接续列表"反映真实意图。真正跨进程地叫停另一个
worker 上的循环需要数据库轮询，那要等向量库切到 Qdrant、部署形态真的变成多
worker 时再补——和 memory 向量库是同一个约束、同一个时机。

## 与断线的关系

``chat_router`` 的 SSE ``finally`` 在客户端断线时会调 ``mark_interrupted``，它
只在状态仍是 ``running`` 时才改。前端"停止"会先发取消请求、再 abort 连接，两条路
无论谁先到都收敛到 ``cancelled``：取消先把状态落成 ``cancelled``，断线那道就看到
非 ``running`` 而不改；断线先把它落成 ``interrupted``，取消那道（``mark_cancelled``
同样接受 ``interrupted``）再把它推成 ``cancelled``。
"""
from __future__ import annotations

import asyncio

# run_id -> 该执行的取消事件。只在循环活着的时候存在（见 register/unregister）。
_tokens: dict[str, asyncio.Event] = {}


def register(run_id: str) -> asyncio.Event:
    """循环开始时登记，返回它的取消事件。

    用 ``setdefault`` 而不是直接覆盖：万一在登记之前就已经有人 ``signal`` 过
    （极窄的竞态），这里要拿到的是同一个已置位的事件，而不是把它换成一个全新的
    未置位事件、从而丢掉那次取消。
    """
    return _tokens.setdefault(run_id, asyncio.Event())


def signal(run_id: str) -> bool:
    """请求取消。返回**这个进程里有没有活着的循环**能收到。

    ``True``：找到了事件并已置位，循环会在下一个安全点自己收尾。
    ``False``：本进程没有在跑它（已结束、断线、或在别的 worker 上）——调用方
    据此退回到"直接落库"那条路。
    """
    event = _tokens.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def is_cancelled(run_id: str) -> bool:
    """循环在安全点查这个。没登记过就是没被取消。"""
    event = _tokens.get(run_id)
    return event is not None and event.is_set()


def unregister(run_id: str) -> None:
    """循环结束（正常、失败、取消、或被断线回收）时摘掉登记。

    必须成对调用，否则这张表会随执行数无限增长。放在 ``_drive_loop`` 的
    ``finally`` 里，所以连客户端断线导致生成器被回收的情况也会走到。
    """
    _tokens.pop(run_id, None)


def live_runs() -> set[str]:
    """当前进程里活着的执行 id。给测试与健康检查用。"""
    return set(_tokens)


__all__ = [
    "is_cancelled",
    "live_runs",
    "register",
    "signal",
    "unregister",
]
