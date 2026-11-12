"""`core.py` 的对外契约。

这些是「一旦破了就会静默出错」的约定：
- 异常类必须**全局唯一**——两处定义会让 `except` 抓不到，任务卡住而不是进 needs_review；
- `atomic_write` 越界必须翻译成 `UncertainState`，而不是把 `UnsafePath` 漏给上层；
- `dumps` 中文不转义、紧凑分隔符，台账体积与可读性都依赖它。
"""
import json

import pytest

from strimkeep import audio, core, media, organizer


def test_exception_classes_are_single_objects():
    """core/organizer/media/audio 看到的必须是同一个类。"""
    assert organizer.UncertainState is core.UncertainState
    assert media.NeedsReview is core.NeedsReview
    assert audio.NeedsReview is core.NeedsReview


def test_atomic_write_translates_unsafe_path_to_uncertain_state(tmp_path):
    """越界写入必须变成 `UncertainState` 且**不吞掉**原始原因。"""
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside.txt'
    with pytest.raises(core.UncertainState) as exc:
        core.atomic_write(outside, 'x', root)
    assert str(exc.value)          # 有可读原因，不是空异常
    assert not outside.exists()    # 越界时绝不能已经写下文件


def test_atomic_write_accepts_str_and_bytes(tmp_path):
    """fsutil 只收 bytes；兼容层必须把 str 按 UTF-8 编码，否则调用方运行期炸 TypeError。"""
    target = tmp_path / 'a.strm'
    core.atomic_write(target, 'http://x/测试.mkv', tmp_path)
    assert target.read_text(encoding='utf-8') == 'http://x/测试.mkv'
    core.atomic_write(target, b'raw-bytes', tmp_path)
    assert target.read_bytes() == b'raw-bytes'
    assert core.atomic_write(target, 'again', tmp_path) == target


def test_dumps_is_compact_and_keeps_chinese():
    text = core.dumps({'名字': '测试电影', 'n': 1})
    assert json.loads(text) == {'名字': '测试电影', 'n': 1}
    assert '测试电影' in text          # 不能被转义成 \uXXXX
    assert ': ' not in text and ', ' not in text   # 紧凑分隔符（台账体积）
