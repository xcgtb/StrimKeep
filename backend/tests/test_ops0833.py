"""0.8.33 回归：深度删除按季折叠 / 电影单片、批完成 TG 通知、附属文件统计字段。

覆盖：
- `deep_delete._group_items`：剧集按季、电影按文件夹、分享单片聚合，计数正确；
- `deep_delete.history`：每批返回 groups + rollup，且不受旧 100 条截断影响；
- `deep_delete.notify_batch`：预演不发通知，真删模式发一条汇总（成功/跳过/失败）。
"""
import asyncio
import time

from strimkeep.deep_delete import _group_items, history, notify_batch
from strimkeep.store import Store

PASSWORD = 'test-password-1234'


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', PASSWORD)


def _insert(store, batch, kind, file_id, playback_id, relative_path, share_id,
            dry_run, state, message=''):
    now = time.time()
    with store.db:
        store.db.execute(
            'INSERT INTO deep_delete_queue(batch,kind,file_id,playback_id,relative_path,'
            'share_id,source,dry_run,state,message,created,updated) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (batch, kind, file_id, playback_id, relative_path, share_id, '',
             int(dry_run), state, message, now, now))


def test_group_items_season_movie_share():
    items = [
        {'relative_path': '媒体库/剧集/欧美剧集/剧A {tmdb-1}/Season 1/剧A.2024.S01E01.第1集.strm',
         'share_id': '', 'file_id': 'f1', 'state': 'done', 'message': ''},
        {'relative_path': '媒体库/剧集/欧美剧集/剧A {tmdb-1}/Season 1/剧A.2024.S01E02.第2集.strm',
         'share_id': '', 'file_id': 'f2', 'state': 'skipped', 'message': 'x'},
        {'relative_path': '媒体库/电影/欧美电影/电影B {tmdb-2}/电影B - 2160p.mkv.strm',
         'share_id': '', 'file_id': 'f3', 'state': 'failed', 'message': 'y'},
        {'relative_path': '', 'share_id': 's1', 'file_id': '', 'state': 'done', 'message': ''},
    ]
    groups = _group_items(items)
    by_kind = {g['kind']: g for g in groups}
    assert set(by_kind) == {'season', 'single', 'share'}

    season = by_kind['season']
    assert season['total'] == 2 and season['done'] == 1 and season['skipped'] == 1
    assert season['title'] == '剧A {tmdb-1}' and season['season'] == 'Season 1'
    assert season['items_total'] == 2 and len(season['items']) == 2

    movie = by_kind['single']
    assert movie['total'] == 1 and movie['failed'] == 1
    assert movie['title'] == '电影B {tmdb-2}'

    assert by_kind['share']['title'] == 's1'


def test_history_returns_groups_and_rollup(tmp_path):
    store = make_store(tmp_path)
    _insert(store, 'b1', 'managed', 'f1', 'p1',
            '媒体库/剧集/欧美剧集/剧A {tmdb-1}/Season 1/剧A.2024.S01E01.strm', '', 0, 'done')
    _insert(store, 'b1', 'managed', 'f2', 'p2',
            '媒体库/剧集/欧美剧集/剧A {tmdb-1}/Season 1/剧A.2024.S01E02.strm', '', 0, 'skipped')
    _insert(store, 'b1', 'managed', 'f3', 'p3',
            '媒体库/电影/欧美电影/电影B {tmdb-2}/电影B.mkv.strm', '', 0, 'failed')
    result = history(store, 10)
    assert len(result['batches']) == 1
    batch = result['batches'][0]
    assert batch['rollup']['files'] == 3
    assert batch['rollup']['seasons'] == 1
    assert batch['rollup']['works'] == 2
    assert {g['kind'] for g in batch['groups']} == {'season', 'single'}
    store.close()


class _FakeNotifications:
    def __init__(self):
        self.calls = []

    async def publish(self, key, title, message, level='info', **kwargs):
        self.calls.append((key, title, message, level))


class _FakeEngine:
    def __init__(self, store):
        self.store = store
        self.notifications = _FakeNotifications()


def test_notify_batch_skips_dry_run(tmp_path):
    store = make_store(tmp_path)
    _insert(store, 'b1', 'managed', 'f1', 'p1', '媒体库/电影/a.strm', '', 1, 'done')
    engine = _FakeEngine(store)
    asyncio.run(notify_batch(engine, 'b1'))
    assert engine.notifications.calls == []
    store.close()


def test_notify_batch_publishes_on_real_delete(tmp_path):
    store = make_store(tmp_path)
    _insert(store, 'b2', 'managed', 'f1', 'p1', '媒体库/电影/a.strm', '', 0, 'done')
    _insert(store, 'b2', 'managed', 'f2', 'p2', '媒体库/电影/b.strm', '', 0, 'skipped')
    engine = _FakeEngine(store)
    asyncio.run(notify_batch(engine, 'b2'))
    assert len(engine.notifications.calls) == 1
    _key, title, message, _level = engine.notifications.calls[0]
    assert title == '🧹 深度删除完成'
    assert '成功 1' in message and '跳过 1' in message
    store.close()
