"""库刷新兜底：目标库唯一性、重叠拒绝、限频合并、失败与未入库的保守结论。"""
import asyncio
import json
import time

import httpx

from strimkeep.emby import EmbyDelivery
from strimkeep.organizer import Organizer
from strimkeep.store import Store, digest
from test_organize import MemoryCloud

CONNECTION = ('http://emby.local', 'k')
TV_ROOT = '/strimkeep/影视媒体库/剧集'
MOVIE_ROOT = '/strimkeep/影视媒体库/电影'


def make_engine(tmp_path, folders, items=None, refresh_status=204, sizes=None):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'playback_base': 'http://nas:9000',
                    'emby_url': CONNECTION[0], 'emby_key': CONNECTION[1]})
    cloud = MemoryCloud(store)
    calls = []
    state = {'items': list(items or [])}
    counts = dict(sizes or {})

    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        if request.url.path.endswith('/Library/VirtualFolders'):
            return httpx.Response(200, json=folders)
        if request.url.path.endswith('/Items/Refresh') or request.url.path.endswith('/Refresh'):
            return httpx.Response(refresh_status)
        if request.url.path.endswith('/Library/Media/Updated'):
            return httpx.Response(204)
        if request.url.path.endswith('/Items'):
            parent = request.url.params.get('ParentId')
            if parent in counts:
                return httpx.Response(200, json={'Items': [], 'TotalRecordCount': counts[parent]})
            return httpx.Response(200, json={'Items': state['items'], 'TotalRecordCount': len(state['items'])})
        return httpx.Response(404, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = Organizer(store, cloud, http, tmp_path / 'strm')
    engine.emby.connection = lambda: CONNECTION
    return store, engine, http, calls, state


def target(pid, name, kind='movie', tmdb=1, root=MOVIE_ROOT):
    return {'relative': f'{name}.strm', 'emby_path': f'{root}/{name}.strm',
            'update_type': 'Created', 'pid': pid, 'kind': kind, 'tmdb': tmdb,
            'title': name, 'confirmed': False}


def insert_delivery(store, targets, state='accepted', attempts=1, sent_ago=0, deadline=600):
    jid = store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('completed','{}',1,1)").lastrowid
    payload = {'targets': targets, 'connection': digest('|'.join(CONNECTION)),
               'ready_at': time.time() - sent_ago, 'sent_at': time.time() - sent_ago,
               'updates': [{'Path': t['emby_path'], 'UpdateType': 'Created'} for t in targets]}
    with store.db:
        store.db.execute("INSERT INTO emby_deliveries(job_id,state,payload,attempts,next_due,deadline) VALUES(?,?,?,?,?,?)",
                         (jid, state, json.dumps(payload, ensure_ascii=False), attempts, 0, time.time() + deadline))
    return jid


def refresh_calls(calls):
    return [path for _, path, _ in calls if path.endswith('/Refresh')]


def events(store):
    return [row[0] for row in store.db.execute("SELECT message FROM events WHERE kind='emby'")]


def test_scan_targets_matching_library_and_shared_root(tmp_path):
    """同库根被正式库和测试库共用时按 ID 固定选一个，不重复刷新。"""
    async def run():
        folders = [
            {'Name': '电视节目', 'Locations': [TV_ROOT], 'ItemId': '900', 'CollectionType': 'tvshows'},
            {'Name': 'StrimKeep测试剧集', 'Locations': [TV_ROOT], 'ItemId': '800', 'CollectionType': 'tvshows'},
            {'Name': '影片', 'Locations': [MOVIE_ROOT], 'ItemId': '700', 'CollectionType': 'movies'},
        ]
        store, engine, http, calls, state = make_engine(tmp_path, folders)
        targets = [target('p1', 'A', kind='tv', root=TV_ROOT + '/欧美剧集/剧名 (2020) {tmdb-1}/Season 1'),
                   target('p2', 'B', kind='tv', root=TV_ROOT + '/欧美剧集/剧名 (2020) {tmdb-1}/Season 1')]
        jid = insert_delivery(store, targets, sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        await engine.emby.tick()
        assert refresh_calls(calls) == ['/Items/800/Refresh']
        row = engine.emby.row(jid)
        assert row['state'] in ('accepted', 'checking')
        assert '已请求刷新媒体库「StrimKeep测试剧集」' in row['message']
        assert json.loads(row['payload'])['scans'][0]['pending'] == 2
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_same_root_prefers_the_lighter_library(tmp_path):
    """同库根的两个库都能刷新该目录时，优先刷新条目更少的那个。"""
    async def run():
        folders = [
            {'Name': '电视节目', 'Locations': [TV_ROOT], 'ItemId': '700', 'CollectionType': 'tvshows'},
            {'Name': 'StrimKeep测试剧集', 'Locations': [TV_ROOT], 'ItemId': '900', 'CollectionType': 'tvshows'},
        ]
        store, engine, http, calls, _ = make_engine(tmp_path, folders, sizes={'700': 5200, '900': 12})
        jid = insert_delivery(store, [target('p1', 'A', kind='tv', root=TV_ROOT + '/欧美剧集/剧名')],
                              sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        await engine.emby.tick()
        assert refresh_calls(calls) == ['/Items/900/Refresh']
        assert 'StrimKeep测试剧集' in engine.emby.row(jid)['message']
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_no_scan_when_path_is_outside_every_library(tmp_path):
    async def run():
        store, engine, http, calls, _ = make_engine(tmp_path, [
            {'Name': '影片', 'Locations': [MOVIE_ROOT], 'ItemId': '700', 'CollectionType': 'movies'}])
        jid = insert_delivery(store, [target('p1', 'A', root='/media/未挂载')],
                              sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        await engine.emby.tick()
        assert refresh_calls(calls) == []
        assert engine.emby.row(jid)['state'] == 'unconfirmed'
        await asyncio.gather(*engine.notice_tasks)
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_overlapping_roots_are_refused_without_scan(tmp_path):
    async def run():
        folders = [
            {'Name': '电视节目', 'Locations': [TV_ROOT], 'ItemId': '900', 'CollectionType': 'tvshows'},
            {'Name': '总库', 'Locations': [TV_ROOT + '/欧美剧集'], 'ItemId': '901', 'CollectionType': 'tvshows'},
        ]
        store, engine, http, calls, _ = make_engine(tmp_path, folders)
        jid = insert_delivery(store, [target('p1', 'A', kind='tv', root=TV_ROOT + '/欧美剧集/剧名')],
                              sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        await engine.emby.tick()
        assert refresh_calls(calls) == []
        assert engine.emby.row(jid)['state'] in ('accepted', 'checking')
        assert any('根路径重叠且不一致' in message for message in events(store))
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_mixed_library_types_are_refused(tmp_path):
    async def run():
        folders = [
            {'Name': '电视节目', 'Locations': [TV_ROOT], 'ItemId': '900', 'CollectionType': 'tvshows'},
            {'Name': '影片', 'Locations': [TV_ROOT], 'ItemId': '700', 'CollectionType': 'movies'},
        ]
        store, engine, http, calls, _ = make_engine(tmp_path, folders)
        jid = insert_delivery(store, [target('p1', 'A', kind='tv', root=TV_ROOT + '/欧美剧集/剧名')],
                              sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        await engine.emby.tick()
        assert refresh_calls(calls) == []
        assert engine.emby.row(jid)['state'] in ('accepted', 'checking')
        assert any('类型不一致' in message for message in events(store))
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_two_jobs_in_the_same_library_share_one_scan(tmp_path):
    async def run():
        folders = [{'Name': '电视节目', 'Locations': [TV_ROOT], 'ItemId': '900', 'CollectionType': 'tvshows'}]
        store, engine, http, calls, _ = make_engine(tmp_path, folders)
        first = insert_delivery(store, [target('p1', 'A', kind='tv', root=TV_ROOT + '/欧美剧集/一剧')],
                                sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        second = insert_delivery(store, [target('p2', 'B', kind='tv', root=TV_ROOT + '/欧美剧集/二剧')],
                                 sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        await engine.emby.tick()
        await engine.emby.tick()
        assert refresh_calls(calls) == ['/Items/900/Refresh']
        assert engine.emby.row(first)['state'] in ('accepted', 'checking')
        assert engine.emby.row(second)['state'] in ('accepted', 'checking')
        assert any('刚刷新过' in message for message in events(store))
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_scan_is_not_requested_before_the_waiting_window(tmp_path):
    async def run():
        folders = [{'Name': '影片', 'Locations': [MOVIE_ROOT], 'ItemId': '700', 'CollectionType': 'movies'}]
        store, engine, http, calls, _ = make_engine(tmp_path, folders)
        jid = insert_delivery(store, [target('p1', 'A')], sent_ago=5)
        await engine.emby.tick()
        assert refresh_calls(calls) == []
        assert engine.emby.row(jid)['state'] == 'accepted'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_refresh_failure_keeps_pending_instead_of_reporting_success(tmp_path):
    async def run():
        folders = [{'Name': '影片', 'Locations': [MOVIE_ROOT], 'ItemId': '700', 'CollectionType': 'movies'}]
        store, engine, http, calls, _ = make_engine(tmp_path, folders, refresh_status=500)
        jid = insert_delivery(store, [target('p1', 'A')], sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        await engine.emby.tick()
        row = engine.emby.row(jid)
        assert refresh_calls(calls) == ['/Items/700/Refresh']
        assert row['state'] in ('accepted', 'checking')
        assert 'HTTP 500' in (json.loads(row['payload']).get('scan_reason') or '')
        assert engine.job(jid)['notify_state'] != 'verified'
        assert any('失败' in message and '保留待核查' in message for message in events(store))
        # 失败后短时间内不重复请求。
        await engine.emby.tick(time.time() + 5)
        assert refresh_calls(calls) == ['/Items/700/Refresh']
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_accepted_scan_does_not_count_as_ingested_until_items_appear(tmp_path):
    async def run():
        folders = [{'Name': '影片', 'Locations': [MOVIE_ROOT], 'ItemId': '700', 'CollectionType': 'movies'}]
        store, engine, http, calls, state = make_engine(tmp_path, folders)
        start = time.time()
        jid = insert_delivery(store, [target('p1', 'A')], sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        await engine.emby.tick()
        assert refresh_calls(calls) == ['/Items/700/Refresh']
        assert engine.emby.row(jid)['state'] == 'checking'
        assert engine.job(jid)['notify_state'] == 'checking'
        # 刷新被接受但媒体库里仍然没有条目：继续核对，不能给 verified。
        await engine.emby.tick(start + 60)
        assert engine.emby.row(jid)['state'] == 'checking'
        assert engine.job(jid)['notify_state'] != 'verified'
        # 到点按实际完成度给未确认，而不是成功。
        await engine.emby.tick(start + 700)
        row = engine.emby.row(jid)
        assert row['state'] == 'unconfirmed' and '未确认' in row['message']
        assert engine.job(jid)['notify_state'] == 'unconfirmed'
        await asyncio.gather(*engine.notice_tasks)
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_items_found_after_scan_flip_delivery_to_verified(tmp_path):
    async def run():
        folders = [{'Name': '影片', 'Locations': [MOVIE_ROOT], 'ItemId': '700', 'CollectionType': 'movies'}]
        store, engine, http, calls, state = make_engine(tmp_path, folders)
        jid = insert_delivery(store, [target('p1', 'A')], sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        await engine.emby.tick()
        assert engine.emby.row(jid)['state'] == 'checking'
        state['items'] = [{'Path': MOVIE_ROOT + '/A.strm',
                           'MediaSources': [{'Path': 'http://nas:9000/play/p1/a'}]}]
        await engine.emby.tick(time.time() + 30)
        row = engine.emby.row(jid)
        assert row['state'] == 'verified'
        assert engine.job(jid)['notify_state'] == 'verified'
        assert refresh_calls(calls) == ['/Items/700/Refresh']
        await asyncio.gather(*engine.notice_tasks)
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_scan_max_per_job_and_cooldown_allow_one_more_after_window(tmp_path):
    async def run():
        folders = [{'Name': '影片', 'Locations': [MOVIE_ROOT], 'ItemId': '700', 'CollectionType': 'movies'}]
        store, engine, http, calls, _ = make_engine(tmp_path, folders)
        jid = insert_delivery(store, [target('p1', 'A')], sent_ago=EmbyDelivery.SCAN_AFTER_SECONDS + 5)
        start = time.time()
        await engine.emby.tick()
        assert refresh_calls(calls) == ['/Items/700/Refresh']
        await engine.emby.tick(start + EmbyDelivery.SCAN_COOLDOWN_SECONDS + 5)
        assert len(refresh_calls(calls)) == 2
        await engine.emby.tick(start + 2 * EmbyDelivery.SCAN_COOLDOWN_SECONDS + 10)
        assert len(refresh_calls(calls)) == 2  # 每个任务最多 2 次
        await http.aclose()
        store.close()
    asyncio.run(run())
