"""共享的状态异常与小工具：子模块不必再从 organizer 里取这些符号。

这些符号原来定义在 `organizer.py`，子模块为了拿它们只能在函数里
`from .organizer import ...`，于是形成 5 条 hub 回指（sync / transfer /
upgrade / share_service / outputs）。搬到独立模块后，organizer 只做 re-export，
子模块改成顶层 import 本模块，环自然消失。

注意：`UncertainState` 必须**只在这里定义一次**——`except` 靠对象标识匹配，
出现第二份定义会让任务卡住而不是进 `needs_review`。
"""
import json
from pathlib import Path

from .fsutil import UnsafePath, atomic_write as _write_atomic


class Stopped(Exception):
    """任务被暂停或取消：向上冒泡，由流水线收尾。"""


class Discarded(Exception):
    """洗版判定为「保留现有」：新文件应回收，而不是丢进待处理。"""


class UncertainState(Exception):
    """状态不确定：需要人工核查，既不能当成功也不能当失败。"""


class NeedsReview(Exception):
    """命名/识别信息不足或不安全：需要人工确认，不能静默降级。

    原来定义在 `media.py`，但 `audio.py` 也要用它（只是为了 `except`），
    于是 `audio -> media` 与 `media -> audio`（`technical_name` 里取
    `naming_track`）构成唯一一条残留的互相依赖。搬到本模块后两边都只依赖
    `core`，`media` 再顶层 `from .audio import naming_track` 也不会成环。
    """


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def atomic_write(path, content, root):
    """保留旧入口：越界或符号链接仍然停手，实现统一到 fsutil。

    `fsutil` 已在模块顶层导入（它是纯叶子，只有 os/secrets/pathlib），
    这样解释器退出阶段就不会再有函数内 import —— 那些运行期 import
    在 `sys.meta_path` 已置 None 时会让后台线程卡在导入锁上。

    `content` 兼容 str 与 bytes：fsutil 只收 bytes，直接透传会让调用方在运行期
    撞上 `TypeError: a bytes-like object is required`。这里统一按 UTF-8 编码，
    对已有传 bytes 的调用方无影响。
    """
    if isinstance(content, str):
        content = content.encode('utf-8')
    try:
        _write_atomic(path, content, root)
    except UnsafePath as exc:
        raise UncertainState(str(exc)) from None
    return Path(path)
