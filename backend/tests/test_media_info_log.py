"""媒体信息失败清单：能回答"哪一部没写进去、为什么"，且成功后自动消失。"""
import json
import sqlite3

from strimkeep.emby_media_inject import Injector
from strimkeep.media_info_log import REASONS, clear, listing, record, summary


class Store:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row


def test_record_keeps_latest_state_and_counts_attempts():
    store = Store()
    record(store, '101', '/media/电影/甲/甲.strm', 'sidecar_missing')
    record(store, '101', '/media/电影/甲/甲.strm', 'sidecar_unreadable')
    items = listing(store)
    assert len(items) == 1                      # 同一条目只留一行，不越积越多
    item = items[0]
    assert item['item_id'] == '101' and item['attempts'] == 2
    assert item['reason'] == 'sidecar_unreadable'          # 原因取最新
    assert item['reason_text'] == REASONS['sidecar_unreadable']
    assert item['name'] == '甲.strm'                        # 没给名字时用路径末段
    assert item['first_at'] <= item['last_at']

    # 成功即清：清单里只保留"当前还没补上"的
    record(store, '101', '/media/电影/甲/甲.strm', '')
    assert summary(store)['count'] == 0


def test_clear_one_or_all():
    store = Store()
    record(store, '1', '/media/a.strm', 'no_streams')
    record(store, '2', '/media/b.strm', 'write_failed')
    assert clear(store, '1') == 1 and summary(store)['count'] == 1
    assert clear(store) == 1 and summary(store)['count'] == 0


def test_blank_item_id_is_ignored():
    store = Store()
    record(store, '', '/media/x.strm', 'no_streams')
    assert summary(store)['count'] == 0


def _library(tmp_path):
    """最小 Emby 库：三条 STRM（正常 / 坏 JSON / 没有流）。"""
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
    conn.execute('INSERT INTO MediaItems (Id, Path) VALUES (3, ?)', ('/media/丙/丙.strm',))
    conn.commit()
    conn.close()

    strm_root = tmp_path / 'strm'
    (strm_root / '甲').mkdir(parents=True)
    (strm_root / '乙').mkdir(parents=True)
    (strm_root / '丙').mkdir(parents=True)
    sidecar = {'streams': [{'Type': 'Video', 'Codec': 'hevc', 'Width': 1920, 'Height': 1080}],
               'chapters': [], 'bitrate': 5000000}
    (strm_root / '甲' / '甲-media-info.json').write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding='utf-8')
    (strm_root / '乙' / '乙-media-info.json').write_text('{不是 JSON', encoding='utf-8')
    (strm_root / '丙' / '丙-media-info.json').write_text('{}', encoding='utf-8')  # 没有流
    return db, strm_root


def test_injector_reports_each_item_and_feeds_the_list(tmp_path):
    db, strm_root = _library(tmp_path)
    store = Store()
    seen = []

    def recorder(item_id, path, reason):
        seen.append(reason)
        record(store, item_id, path, reason)

    injector = Injector(str(db), strm_root=strm_root, recorder=recorder)
    stats = injector.run(backup=False)

    assert stats['injected'] == 1 and stats['failed'] == 2
    assert [item['reason'] for item in stats['failures']] == \
        ['sidecar_unreadable', 'no_streams']
    assert seen == ['', 'sidecar_unreadable', 'no_streams']      # 逐条回报，成功也回
    assert sorted(item['reason'] for item in listing(store)) == ['no_streams',
                                                                'sidecar_unreadable']

    # 补上 sidecar 后再跑一次：成功会把自己那条失败记录清掉
    (tmp_path / 'strm' / '乙' / '乙-media-info.json').write_text(
        json.dumps({'streams': [{'Type': 'Video', 'Codec': 'h264'}]}), encoding='utf-8')
    again = injector.run(backup=False)
    assert again['injected'] == 1
    assert [item['item_id'] for item in listing(store)] == ['3']
