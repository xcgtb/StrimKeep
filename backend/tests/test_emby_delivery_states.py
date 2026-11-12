"""Emby 交付的三态判定、库根预检与重复入库识别。"""
import asyncio
import json
import time

import httpx
import pytest

from strimkeep.emby import EmbyDelivery
from strimkeep.organizer import Organizer
from strimkeep.store import Store, digest
from test_organize import MemoryCloud

CONNECTION = ('http://emby.local', 'k')


def make_engine(tmp_path, routes):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'playback_base': 'http://nas:9000',
                    'emby_url': CONNECTION[0], 'emby_key': CONNECTION[1]})
    cloud = MemoryCloud(store)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        body = routes.get((request.method, request.url.path))
        if body is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=body) if isinstance(body, (dict, list)) else httpx.Response(200)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = Organizer(store, cloud, http, tmp_path / 'strm')
    engine.emby.connection = lambda: CONNECTION
    return store, engine, http, calls


def target(pid, name, kind='movie', tmdb=1):
    return {'relative': f'{name}.strm', 'emby_path': f'/strimkeep/影视媒体库/电影/{name}.strm',
            'update_type': 'Created', 'pid': pid, 'kind': kind, 'tmdb': tmdb,
            'title': name, 'confirmed': False}


def insert_delivery(store, targets, updates, state='accepted', attempts=1, deadline=None):
    jid = store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('completed','{}',1,1)").lastrowid
    payload = {'targets': targets, 'connection': digest('|'.join(CONNECTION)),
               'ready_at': time.time(), 'sent_at': time.time() - 1800, 'updates': updates}
    with store.db:
        store.db.execute("INSERT INTO emby_deliveries(job_id,state,payload,attempts,next_due,deadline) VALUES(?,?,?,?,?,?)",
                         (jid, state, json.dumps(payload, ensure_ascii=False), attempts, 0,
                          deadline if deadline is not None else time.time() - 1))
    return jid


def test_covered_by_matches_prefix_only():
    folders = [{'name': '影片', 'locations': ['/strimkeep/影视媒体库/电影']}]
    assert EmbyDelivery.covered_by(folders, '/strimkeep/影视媒体库/电影/a.strm')
    assert EmbyDelivery.covered_by(folders, '/strimkeep/影视媒体库/电影')
    assert not EmbyDelivery.covered_by(folders, '/strimkeep/影视媒体库/电影番外/a.strm')
    assert not EmbyDelivery.covered_by(folders, '/strm/other/a.strm')


def test_deadline_state_reports_partial_instead_of_blank_failure():
    state, message, _ = EmbyDelivery.deadline_state({'total': 30, 'confirmed': 29, 'duplicates': []})
    assert state == 'partial' and '29/30' in message
    state, message, _ = EmbyDelivery.deadline_state({'total': 30, 'confirmed': 0, 'duplicates': []})
    assert state == 'unconfirmed'
    state, _, _ = EmbyDelivery.deadline_state(None)
    assert state == 'unconfirmed'


def test_partial_delivery_keeps_counts(tmp_path):
    async def run():
        routes = {('GET', '/Items'): {'Items': [{'Path': '/x/a.strm',
                  'MediaSources': [{'Path': 'http://nas:9000/play/p1/a'}]}], 'TotalRecordCount': 1}}
        store, engine, http, _ = make_engine(tmp_path, routes)
        targets = [target('p1', 'A'), target('p2', 'B')]
        jid = insert_delivery(store, targets, [])
        await engine.emby.tick()
        row = engine.emby.row(jid)
        assert row['state'] == 'partial'
        assert '1/2' in row['message']
        assert engine.job(jid)['notify_state'] == 'partial'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_missing_library_root_is_diagnosed_on_first_failed_check(tmp_path):
    async def run():
        routes = {
            ('GET', '/Library/VirtualFolders'):
                [{'Name': '影片', 'Locations': ['/strimkeep/影视媒体库/电影']}],
            ('GET', '/Items'): {'Items': [], 'TotalRecordCount': 0},
        }
        store, engine, http, calls = make_engine(tmp_path, routes)
        missing = target('p1', 'A')
        missing['emby_path'] = '/media/未挂载/A.strm'
        jid = insert_delivery(store, [missing], [], state='accepted', attempts=1,
                              deadline=time.time() + 600)
        await engine.emby.tick()
        row = engine.emby.row(jid)
        assert row['state'] == 'unconfirmed'
        assert '不在任何 Emby 媒体库下' in row['message']
        assert not any(path == '/Library/Media/Updated' for _, path in calls)
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_verified_delivery_reports_duplicate_entries_in_upstream_call_order(tmp_path):
    async def run():
        routes = {
            ('GET', '/System/Info/Public'): {'ServerName': 'test', 'Version': '4.8'},
            ('GET', '/Library/VirtualFolders'):
                [{'Name': '影片', 'Locations': ['/strimkeep/影视媒体库/电影']}],
            ('POST', '/Library/Media/Updated'): {},
            ('GET', '/Items'): {'Items': [
                {'Path': '/strimkeep/影视媒体库/电影/A.strm',
                 'MediaSources': [{'Path': 'http://nas:9000/play/p1/a'}]},
                {'Path': '/strm/115网盘/影视媒体库/电影/A.strm',
                 'MediaSources': [{'Path': 'http://nas:9000/play/p9/a'}]},
            ], 'TotalRecordCount': 2},
        }
        store, engine, http, calls = make_engine(tmp_path, routes)
        jid = insert_delivery(store, [target('p1', 'A')],
                              [{'Path': '/strimkeep/影视媒体库/电影/A.strm', 'UpdateType': 'Created'}],
                              state='pending', attempts=0, deadline=time.time() + 600)
        await engine.emby.tick()
        assert any(path == '/Library/Media/Updated' for _, path in calls)
        await engine.emby.tick(time.time() + 60)
        row = engine.emby.row(jid)
        assert row['state'] == 'verified'
        assert '多个条目' in row['message']
        paths = [path for _, path in calls]
        assert '/Library/VirtualFolders' in paths
        assert paths.index('/Library/VirtualFolders') < paths.index('/Library/Media/Updated')
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_overlap_is_reported_when_roots_contain_each_other(tmp_path):
    async def run():
        store, engine, http, _ = make_engine(tmp_path, {})
        engine.emby.report_overlap(7, [
            {'name': '影片', 'locations': ['/strimkeep/影视媒体库/电影']},
            {'name': '影片备份', 'locations': ['/strimkeep/影视媒体库']},
        ])
        messages = [row[0] for row in store.db.execute("SELECT message FROM events WHERE kind='emby'")]
        assert any('路径重叠' in message for message in messages)
        await http.aclose()
        store.close()
    asyncio.run(run())
