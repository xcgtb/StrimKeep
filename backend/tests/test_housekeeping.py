"""events 表裁剪：按时间删、再按行数删最老的，dry_run 不动库。"""

import sqlite3
import time


class FakeStore:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                             'at REAL, kind TEXT, message TEXT);')
        self.values = {'events_keep_days': 30, 'events_keep_rows': 100}
        self.events = []

    def get(self, key, default=''):
        return self.values.get(key, default)

    def event(self, kind, message):
        self.events.append((kind, message))


def test_prune_by_age_and_count(tmp_path):
    from strimkeep.housekeeping import prune, stats

    store = FakeStore()
    now = time.time()
    store.db.executemany('INSERT INTO events (at, kind, message) VALUES (?,?,?)',
                         [(now - 100 * 86400, 'system', '太老'),      # 超过 30 天，按时间删
                          (now - 10 * 86400, 'system', '较新')])
    store.db.commit()
    # 再加 150 条「近期但超出 100 行上限」的记录
    store.db.executemany('INSERT INTO events (at, kind, message) VALUES (?,?,?)',
                         [(now - 3600, 'system', '近期 %d' % i) for i in range(150)])
    store.db.commit()
    assert stats(store)['rows'] == 152

    preview = prune(store, dry_run=True)
    assert preview['by_age'] == 1 and preview['by_count'] == 51
    assert store.db.execute('SELECT count(*) FROM events').fetchone()[0] == 152

    result = prune(store)
    assert result['by_age'] == 1 and result['by_count'] == 51
    assert result['remaining'] == 100
    assert store.db.execute("SELECT count(*) FROM events WHERE message='太老'").fetchone()[0] == 0
    assert store.events and '裁剪' in store.events[-1][1]


def test_prune_keeps_everything_when_limits_disabled():
    from strimkeep.housekeeping import prune

    store = FakeStore()
    store.values = {'events_keep_days': 0, 'events_keep_rows': 0}
    now = time.time()
    store.db.executemany('INSERT INTO events (at, kind, message) VALUES (?,?,?)',
                         [(now - 999 * 86400, 'system', '老 %d' % i) for i in range(20)])
    store.db.commit()

    result = prune(store)
    assert result['by_age'] == 0 and result['by_count'] == 0 and result['remaining'] == 20


class ForgetStore:
    """只够 purge_dead_config 用：values 字典 + forget + event。"""

    def __init__(self, values):
        self.values = dict(values)
        self.events = []

    def get(self, key, default=''):
        return self.values.get(key, default)

    def forget(self, keys):
        removed = [key for key in keys if key in self.values]
        for key in removed:
            self.values.pop(key, None)
        return removed

    def event(self, kind, message):
        self.events.append((kind, message))


def test_purge_dead_config_removes_only_dead_keys():
    """旧开关删掉；还在用的设置项一个都不能碰。"""
    from strimkeep.housekeeping import purge_dead_config

    store = ForgetStore({'media_info_extract': True, 'media_info_inject_minutes': 5,
                         'emby_url': 'http://127.0.0.1:8100', 'emby_key': 'k',
                         'sync_config': {'emby_media_info': True}})
    removed = purge_dead_config(store)
    assert set(removed) == {'media_info_extract', 'media_info_inject_minutes'}
    assert set(store.values) == {'emby_url', 'emby_key', 'sync_config'}
    assert store.events and '已清理不再使用的设置项' in store.events[-1][1]
    assert purge_dead_config(store) == []          # 幂等：第二次没有可删的


def test_no_module_reads_dead_settings():
    """静态守约束：死键只能在"清理/迁移"里出现，不能有读取点。

    教训：`emby_proxy_*` 曾同时被规则字段和旧键读取，设置页改了却没效果，
    排查了很久才发现是两套配置并存。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / 'strimkeep'
    hits = {}
    for path in root.glob('*.py'):
        text = path.read_text(encoding='utf-8')
        for key in ('media_info_extract', 'media_info_inject_minutes'):
            assert "get('%s'" % key not in text, '%s 仍在读 %s' % (path.name, key)
        count = text.count("get('emby_proxy_")
        if count:
            hits[path.name] = count
    # 只有迁移函数读这四个老键（enabled/port/upstream/api_key），别处一律不许。
    assert hits == {'emby_proxy.py': 4}
    text = (root / 'emby_proxy.py').read_text(encoding='utf-8')
    head, _, rest = text.partition('def _migrate_legacy')
    tail = rest.partition('\n    def ')[2]
    for chunk in (head, tail):
        assert "get('emby_proxy_" not in chunk
