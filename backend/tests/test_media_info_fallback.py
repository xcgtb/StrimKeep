"""媒体信息兜底链路：cron 那一轮必须真的能跑起来。

0.9.84 把注入器工厂写成了 ``install_media_info`` 里的闭包，而兜底的 ``inject_loop``
是模块级函数 —— 每次启动的首个 tick 都抛 NameError，只留下一行
「兜底写入媒体信息失败：NameError」，看起来像"没问题"。这里锁住两件事：
模块级工厂可用，且源码里不再出现那个闭包名字。
"""
import asyncio
import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        # 兜底那一轮跑在 asyncio.to_thread 里：连接必须允许跨线程（真实 Store 是按线程取连接）
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.values = {'emby_library_db': str(path), 'emby_strm_mount': '/media'}
        self.events = []

    def get(self, key, default=''):
        return self.values.get(key, default)

    def event(self, kind, message):
        self.events.append((kind, message))


class App:
    def __init__(self, strms):
        # 真实 FastAPI 应用把工作目录挂在 app.state.strms 上
        self.state = type('State', (), {'strms': Path(strms)})()


def _library(tmp_path, with_sidecar=True):
    from strimkeep.emby_media_inject import COLUMNS

    columns = [name.strip() for name in COLUMNS.split(',')]
    fields = ', '.join('ItemId INT' if name == 'ItemId'
                       else 'StreamType INT' if name == 'StreamType'
                       else '%s TEXT' % name for name in columns)
    db = tmp_path / 'library.db'
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE MediaItems (Id INTEGER PRIMARY KEY, Path TEXT, Width INT, Height INT,
                                 TotalBitrate INT);
        CREATE TABLE %s;
        CREATE TABLE Chapters3 (ItemId INT, ChapterIndex INT, StartPositionTicks INT, Name TEXT,
                                ImagePath TEXT, ImageDateModified TEXT, MarkerType INT);
    """ % ('MediaStreams2 (%s)' % fields))
    conn.execute('INSERT INTO MediaItems (Id, Path) VALUES (1, ?)', ('/media/甲/甲.strm',))
    conn.execute('INSERT INTO MediaItems (Id, Path) VALUES (2, ?)', ('/media/乙/乙.strm',))
    conn.commit()
    conn.close()

    strm_root = tmp_path / 'strm'
    (strm_root / '甲').mkdir(parents=True)
    (strm_root / '乙').mkdir(parents=True)
    if with_sidecar:
        (strm_root / '甲' / '甲-media-info.json').write_text(
            json.dumps({'streams': [{'Type': 'Video', 'Codec': 'hevc'}]}), encoding='utf-8')
    return db, strm_root


def test_fallback_run_injects_and_records_failures(tmp_path):
    from strimkeep.media_info_api import inject_once

    db, strm_root = _library(tmp_path)
    store = Store(db)
    stats = asyncio.run(inject_once(App(strm_root), store))

    assert stats['ok'] is True and stats['injected'] == 1
    assert stats['failures'] == [{'item_id': 2, 'path': '/media/乙/乙.strm',
                                  'reason': 'sidecar_missing'}]
    # 没有 sidecar 的第二条进了失败清单，而不是只留一个计数
    assert store.db.execute('SELECT item_id FROM media_info_failures').fetchone()[0] == '2'


def test_fallback_without_library_db_fails_loudly(tmp_path):
    from strimkeep.media_info_api import inject_once

    db, strm_root = _library(tmp_path, with_sidecar=False)
    store = Store(db)
    store.values['emby_library_db'] = ''
    try:
        asyncio.run(inject_once(App(strm_root), store))
    except ValueError as exc:
        assert 'emby_library_db' in str(exc)
    else:
        raise AssertionError('未配置库路径时应明确报错')


def test_injector_factory_is_module_level():
    """静态守约束：工厂不能再写回 install 内部的闭包。"""
    source = Path(__file__).resolve().parents[1] / 'strimkeep' / 'media_info_api.py'
    text = source.read_text(encoding='utf-8')
    assert 'def make_injector(' in text
    assert 'injector_for' not in text

def test_fallback_cycle_skips_quietly_when_library_db_missing(tmp_path, monkeypatch):
    """没配置 Emby 数据库路径时兜底要安静跳过，别再记「写入失败」。

    现场：每次启动/到点都写一条「兜底写入媒体信息失败：emby_library_db 未配置」，
    看起来像媒体信息写不进 Emby 的故障；其实这条链路是可选功能。
    """
    from strimkeep.media_info_api import inject_cycle

    monkeypatch.delenv('STRIMKEEP_EMBY_LIBRARY_DB', raising=False)
    db, strm_root = _library(tmp_path, with_sidecar=False)
    store = Store(db)
    store.values['emby_library_db'] = ''
    stats = asyncio.run(inject_cycle(App(strm_root), store))
    assert stats == {'ok': True, 'skipped': 'unconfigured'}
    assert not store.events
