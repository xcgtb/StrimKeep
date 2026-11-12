"""迭代 2A 回归：统一原子写、硬链接兜底、boundary 热路径缓存。

要点：缓存不能削弱「暂停即停」与「账号切换即停」这两个中止条件。
"""
import asyncio
import json
import shutil
import time
from pathlib import Path

import httpx
import pytest

import strimkeep.organizer as organizer_module
from strimkeep import fsutil
from strimkeep.cloud115 import Cloud115
from strimkeep.cover_service import CoverService
from strimkeep.fsutil import UnsafePath, atomic_write, write_once
from strimkeep.organizer import Organizer, Stopped, UncertainState
from strimkeep.store import Store
from test_organize import MemoryCloud


def test_atomic_write_replaces_and_guards_root(tmp_path):
    root = tmp_path / 'strm'
    root.mkdir()
    target = root / 'a' / 'b.strm'
    atomic_write(target, b'first', root)
    assert target.read_bytes() == b'first'
    atomic_write(target, b'second', root)          # 覆盖写，不留 .tmp
    assert target.read_bytes() == b'second'
    assert [p.name for p in target.parent.iterdir()] == ['b.strm']
    with pytest.raises(UnsafePath):
        atomic_write(tmp_path / 'outside.strm', b'x', root)


def test_write_once_falls_back_when_link_unsupported(tmp_path, monkeypatch):
    root = tmp_path / 'strm'
    root.mkdir()
    target = root / 'poster.jpg'

    def refuse(source, destination):
        raise OSError(1, 'Operation not permitted')      # CIFS/NFS 的典型返回

    monkeypatch.setattr(fsutil.os, 'link', refuse)
    assert write_once(target, b'jpeg-bytes', root) is True
    assert target.read_bytes() == b'jpeg-bytes'
    assert write_once(target, b'other', root) is False   # 已存在不覆盖
    assert target.read_bytes() == b'jpeg-bytes'


def test_write_new_uses_fallback_and_keeps_existing(tmp_path, monkeypatch):
    from strimkeep.strm_options import write_new

    root = tmp_path / 'strm'
    root.mkdir()
    target = root / 'movie.nfo'

    def refuse(source, destination):
        raise OSError(22, 'Invalid cross-device link')

    monkeypatch.setattr(fsutil.os, 'link', refuse)
    assert write_new(target, b'<movie/>', root) is True
    assert target.read_bytes() == b'<movie/>'
    assert write_new(target, b'<other/>', root) is False
    assert target.read_bytes() == b'<movie/>'
    with pytest.raises(ValueError):
        write_new(tmp_path / 'nope.nfo', b'x', root)


def make_engine(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                    'playback_base': 'http://nas:9000'})
    (tmp_path / 'strm').mkdir(parents=True, exist_ok=True)   # boundary 会检查这条路径
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    engine = Organizer(store, MemoryCloud(store), http, tmp_path / 'strm')
    with store.db:
        job_id = store.db.execute(
            "INSERT INTO organize_jobs(state,config,created,updated) VALUES('running',?,?,?)",
            (json.dumps({'job_kind': 'organize', 'account_id': '1', 'source_cids': ['100']}),
             time.time(), time.time())).lastrowid
    return store, engine, http, job_id


def test_boundary_caches_disk_check_but_still_stops_on_pause(tmp_path, monkeypatch):
    store, engine, http, job_id = make_engine(tmp_path)
    calls = []
    real = shutil.disk_usage

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(organizer_module.shutil, 'disk_usage', counting)

    engine.boundary(job_id)
    engine.boundary(job_id)
    engine.boundary(job_id)
    # 两个路径各查一次，后续调用走 10 秒缓存
    assert len(calls) == 2, calls

    engine.update_job(job_id, state='paused')
    with pytest.raises(Stopped):
        engine.boundary(job_id)
    asyncio.run(http.aclose())
    store.close()


def test_boundary_detects_account_switch_even_with_cache(tmp_path):
    store, engine, http, job_id = make_engine(tmp_path)
    engine.boundary(job_id)                       # 先填充缓存
    store.set_many({'cookie': 'UID=2; CID=t; SEID=t'})
    with pytest.raises(UncertainState, match='账号'):
        engine.boundary(job_id)
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t'})
    engine.boundary(job_id)
    asyncio.run(http.aclose())
    store.close()


class StubRecycleCloud(Cloud115):
    """只替换 115 调用：回收站分页由固定数据提供。"""

    def __init__(self, store, total):
        super().__init__(store)
        self.total = total
        self.offsets = []

    def client(self):
        return type('Client', (), {'recyclebin_list': object()})()

    async def call(self, method, payload=None, **kwargs):
        offset = int(payload['offset'])
        self.offsets.append(offset)
        size = min(100, self.total - offset)
        page = [{'rid': str(offset + index), 'file_name': f'f{offset + index}',
                 'file_size': 10, 'cid': '1000', 'type': '1', 'dtime': 1700000000}
                for index in range(max(0, size))]
        return {'count': self.total, 'offset': offset, 'data': page}


def test_recycle_recent_is_bounded_and_falls_back(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    cloud = StubRecycleCloud(store, total=250)

    rows, complete = asyncio.run(cloud.recycle_recent(1))
    assert len(rows) == 100 and complete is False
    assert cloud.offsets == [0]                     # 只读了一页

    cloud.offsets.clear()
    rows, complete = asyncio.run(cloud.recycle_recent(3))
    assert len(rows) == 250 and complete is True
    assert cloud.offsets == [0, 100, 200]

    # 全量读会把清单记成快照：下一个文件可以直接复用
    cloud.offsets.clear()
    full = asyncio.run(cloud.recycle_files())
    assert len(full) == 250
    cached = cloud.recycle_snapshot()
    assert cached is not None and len(cached) == 250
    asyncio.run(cloud.close())
    store.close()


def test_startup_creates_every_table_modules_used_to_create(tmp_path):
    """12 张表原先由各模块自建；现在必须启动即建齐。

    0.9.54 起 `output_stats` 与 `media_info_outputs` 两张纯派生表退场（见
    test_store_retires_derived_tables_at_startup），这里断言它们确实不再出现。
    """
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    names = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {'share_strms', 'share_watchlist', 'share_revocations', 'share_wash_intents',
                'channel_state', 'channel_shares', 'channel_posts', 'channel_backfill',
                'offline_submissions', 'native_media_info_queue',
                'emby_outbox', 'deep_delete_queue', 'strm_changes',
                'emby_deliveries'}
    missing = expected - names
    assert not missing, '启动时漏建的表：%s' % sorted(missing)
    assert not ({'output_stats', 'media_info_outputs'} & names), '派生表不该再被建出来'
    assert store.meta('schema_version') == '2', '启动后应把库抬到 schema 版本 2'
    store.close()


def test_prune_drops_finished_queue_rows(tmp_path):
    """历史清理：终态的删除明细按 7 天清掉，排队中的一律保留。"""
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    old = time.time() - 30 * 86400
    with store.db:
        store.db.execute("INSERT INTO deep_delete_queue(batch,kind,state,created,updated)"
                         " VALUES('b1','managed','done',?,?)", (old, old))
        store.db.execute("INSERT INTO deep_delete_queue(batch,kind,state,created,updated)"
                         " VALUES('b2','managed','queued',?,?)", (old, old))
    assert store.prune()['queue'] == 1
    left = store.db.execute('SELECT state FROM deep_delete_queue').fetchall()
    assert [row['state'] for row in left] == ['queued'], '排队中的不能清'
    store.close()


def test_prune_caps_noisy_event_kinds_so_alerts_survive(tmp_path, monkeypatch):
    """0.9.55：高频事件按 kind 单独封顶，别把 emby/system 告警挤出事件表。

    现场 events 打满 2 万时 organize 7343 + progress 3536 + deep_delete 1949 占掉一半
    名额，真正的告警（媒体信息空窗）被全局 FIFO 冲走。明细不会丢——`Store.event()`
    同时写 /data/logs 的按天日志文件，那里保留全部。
    """
    from strimkeep import store as store_module
    monkeypatch.setattr(store_module, 'EVENT_KIND_CAPS', (('progress', 3), ('organize', 2)))
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    now = time.time()
    with store.db:
        for i in range(10):
            store.db.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)',
                             (now, 'progress', 'p%d' % i))
        for i in range(6):
            store.db.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)',
                             (now, 'organize', 'o%d' % i))
        store.db.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)',
                         (now, 'emby', '关键告警'))
    store.prune()
    counts = {}
    for row in store.db.execute('SELECT kind FROM events'):
        counts[row['kind']] = counts.get(row['kind'], 0) + 1
    assert counts.get('progress') == 3 and counts.get('organize') == 2
    assert counts.get('emby') == 1, '告警不能被高频事件挤掉'
    kept = [row['message'] for row in store.db.execute(
        "SELECT message FROM events WHERE kind='progress' ORDER BY id")]
    assert kept == ['p7', 'p8', 'p9'], '保留的是最新的几条'
    store.close()


def test_prune_drops_old_skipped_queue_rows_after_two_days(tmp_path):
    """0.9.56：`skipped` 明细只留 2 天。

    现场 11-04 的一次深度删除爆发留下 12,129 行（6.8MB），按 7 天规则要等满一周；
    而全库 freelist 只有 0.7MB，说明这些是真占地方、VACUUM 也救不回来。
    """
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    three_days = time.time() - 3 * 86400
    one_day = time.time() - 86400
    with store.db:
        store.db.execute("INSERT INTO deep_delete_queue(batch,kind,state,created,updated)"
                         " VALUES('old','managed','skipped',?,?)", (three_days, three_days))
        store.db.execute("INSERT INTO deep_delete_queue(batch,kind,state,created,updated)"
                         " VALUES('new','managed','skipped',?,?)", (one_day, one_day))
        store.db.execute("INSERT INTO deep_delete_queue(batch,kind,state,created,updated)"
                         " VALUES('queued','managed','queued',?,?)", (three_days, three_days))
    assert store.prune()['queue'] == 1
    left = sorted(row['batch'] for row in store.db.execute('SELECT batch FROM deep_delete_queue'))
    assert left == ['new', 'queued'], '2 天内的 skipped 与排队中的都要留'
    store.close()


def test_meta_json_roundtrip(tmp_path):
    """面板「最近一次深度删除」依赖 meta 存 JSON：写进去要能原样读回来。"""
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    payload = {'at': 1, 'mode': '执行', 'details': [{'path': 'a/b.strm', 'actions': ['删除本地 STRM']}]}
    store.set_meta('deep_delete_last', payload)
    assert store.meta_json('deep_delete_last') == payload
    assert store.meta_json('never_written', {'ok': True}) == {'ok': True}
    store.close()


def test_prune_only_clears_old_finished_item_rows(tmp_path):
    """保留策略：只清已完成任务的老明细，跑着的任务与台账一律不动。"""
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    old = time.time() - 90 * 86400
    with store.db:
        store.db.execute("INSERT INTO organize_jobs(id,state,config,created,updated)"
                         " VALUES(1,'completed','{}',?,?)", (old, old))
        store.db.execute("INSERT INTO organize_jobs(id,state,config,created,updated)"
                         " VALUES(2,'running','{}',?,?)", (old, old))
        store.db.execute("INSERT INTO organize_items(job_id,file_id,original,state)"
                         " VALUES(1,'a','{}','done')")
        store.db.execute("INSERT INTO organize_items(job_id,file_id,original,state)"
                         " VALUES(2,'b','{}','pending')")
    result = store.prune(keep_jobs=0)
    assert result['items'] == 1
    left = store.db.execute('SELECT job_id FROM organize_items').fetchall()
    assert [row['job_id'] for row in left] == [2], '跑着的任务明细不能被清'
    # 默认保留最近 20 个任务：再跑一次一条也不该动
    assert store.prune()['items'] == 0
    store.close()


def test_share_organize_no_longer_triggers_full_library_refresh():
    """分享整理要跟整理/同步一样走路径通知，不再整库重扫。"""
    source = (Path(__file__).resolve().parents[1] / 'strimkeep' / 'share_service.py').read_text(encoding='utf-8')
    assert "'/Library/Refresh'" not in source      # 不再拼接全库刷新地址
    assert 'emby.enqueue(job_id)' in source


def test_children_memo_and_ledger_are_scoped(tmp_path):
    """清单缓存与请求账本按「账号 + 任务」隔离：换账号必须重读，账本不能串台。"""
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        cloud = Cloud115(store)
        calls = []

        async def fake_children(cid, page_size=200):
            calls.append(str(cid))
            return ([{'id': '1', 'name': 'a', 'is_dir': True}], 1)

        cloud._children = fake_children
        cloud.use_scope('1', 7)
        await cloud.children('9')
        await cloud.children('9')
        assert calls == ['9'], '同一作用域第二次读要命中缓存'

        cloud.use_scope('2', 8)                      # 换了 115 账号
        await cloud.children('9')
        assert calls == ['9', '9'], '换账号后必须重新读，不能拿上一个账号的清单'

        cloud.use_scope('1', 7)
        await cloud.children('9')
        assert calls == ['9', '9'], '切回原作用域仍应命中自己的缓存'

        cloud.use_scope('1', 7)
        cloud.count_op('改名', 2)
        scope_a = cloud.ledger_summary()
        cloud.use_scope('2', 8)
        assert cloud.ledger_summary()['ops'] == {}, '账本不能把别的任务的计数算进来'
        cloud.use_scope('1', 7)
        assert cloud.ledger_summary()['ops'] == scope_a['ops']
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_cover_service_hosts_background_tasks(tmp_path):
    """封面补齐的后台协程必须留引用，停机时能被收拢。"""
    async def run():
        cover = CoverService(None, tmp_path, None)
        started = asyncio.Event()
        release = asyncio.Event()

        async def work():
            started.set()
            await release.wait()

        task = cover.spawn(work())
        await started.wait()
        assert task in cover.tasks, '后台任务必须留引用，避免被 GC 中途回收'
        await cover.close()
        assert task.done() and task.cancelled()
        assert not [t for t in cover.tasks if not t.done()]
    asyncio.run(run())
