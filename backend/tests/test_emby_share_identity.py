"""分享入库的 Emby 身份：Emby 里是 share/<share_id>，不是 115 文件 ID。"""
import asyncio
import json
import time

import httpx

from strimkeep.emby import EmbyDelivery
from strimkeep.organizer import Organizer
from strimkeep.store import Store, digest
from test_organize import MemoryCloud

CONNECTION = ('http://emby.local', 'k')
SHARE_PATH = '分享影视库/剧集/小城良方 (2026)/Season 1/小城良方 - S01E01.strm'
EPISODE_PATH = 'http://nas:9000/play115/share/s1/%E5%B0%8F%E5%9F%8E%E8%89%AF%E6%96%B9.S01E01.strm'


def make_engine(tmp_path, routes):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'playback_base': 'http://nas:9000',
                    'emby_url': CONNECTION[0], 'emby_key': CONNECTION[1]})
    cloud = MemoryCloud(store)

    def handler(request):
        body = routes.get((request.method, request.url.path))
        if body is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = Organizer(store, cloud, http, tmp_path / 'strm')
    engine.emby.connection = lambda: CONNECTION
    return store, engine, http


def add_share(store, share_id='s1', file_id='115abc'):
    with store.db:
        store.db.execute('INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,'
                         'name,strm_path,token_hash,revoked,created)'
                         " VALUES(?,?,?,?,?,?,?,?,0,1)",
                         (share_id, 'c1', '', file_id, 'pc', '小城良方 (2026)', SHARE_PATH, 'h'))


def add_job(store):
    jid = store.db.execute("INSERT INTO organize_jobs(state,config,created,updated)"
                           " VALUES('completed','{}',1,1)").lastrowid
    plan = {'relative_path': SHARE_PATH, 'hint': {'media_type': 'tv', 'tmdb_id': 285625},
            'details': {'title': '小城良方', 'id': 285625}}
    with store.db:
        store.db.execute('INSERT INTO organize_items(job_id,file_id,original,plan,state)'
                         " VALUES(?,?,'{}',?,'done')",
                         (jid, '115abc', json.dumps(plan, ensure_ascii=False)))
    return jid


def insert_delivery(store, jid, pid, state='accepted', attempts=1, deadline=None):
    payload = {'targets': [{'relative': SHARE_PATH, 'emby_path': '/strimkeep/' + SHARE_PATH,
                            'update_type': 'Created', 'pid': pid, 'kind': 'tv',
                            'tmdb': 285625, 'title': '小城良方', 'confirmed': False}],
               'connection': digest('|'.join(CONNECTION)), 'ready_at': time.time(),
               'sent_at': time.time() - 60}
    with store.db:
        store.db.execute('INSERT INTO emby_deliveries(job_id,state,payload,attempts,next_due,deadline)'
                         ' VALUES(?,?,?,?,0,?)',
                         (jid, state, json.dumps(payload, ensure_ascii=False), attempts,
                          deadline if deadline is not None else time.time() + 60))


def test_targets_use_share_identity(tmp_path):
    store, engine, _ = make_engine(tmp_path, {})
    add_share(store)
    jid = add_job(store)
    targets = engine.emby.targets(jid)
    assert [t['pid'] for t in targets] == ['share/s1']


def test_matches_accepts_share_path_only():
    item = {'Path': EPISODE_PATH}
    assert EmbyDelivery.matches(item, {'pid': 'share/s1', 'relative': SHARE_PATH})
    # 115 文件 ID 不该再被当成分享 STRM 的身份。
    assert not EmbyDelivery.matches(item, {'pid': '115abc', 'relative': SHARE_PATH})


def test_verify_rewrites_legacy_pid_and_confirms(tmp_path):
    routes = {('GET', '/Items'): {'Items': [{'Id': 'series1',
                                             'ProviderIds': {'Tmdb': '285625'}}],
                                  'TotalRecordCount': 1},
              ('GET', '/Shows/series1/Episodes'): {'Items': [{'Id': 'ep1',
                                                              'Path': EPISODE_PATH}],
                                                   'TotalRecordCount': 1}}
    store, engine, _ = make_engine(tmp_path, routes)
    add_share(store)
    jid = add_job(store)
    insert_delivery(store, jid, pid='115abc')

    progress = asyncio.run(engine.emby.verify(jid))
    assert progress == {'total': 1, 'confirmed': 1, 'duplicates': []}
    payload = json.loads(store.db.execute('SELECT payload FROM emby_deliveries WHERE job_id=?',
                                          (jid,)).fetchone()['payload'])
    assert payload['targets'][0]['pid'] == 'share/s1'


def test_repair_rearms_unconfirmed_delivery(tmp_path):
    store, engine, _ = make_engine(tmp_path, {})
    add_share(store)
    jid = add_job(store)
    insert_delivery(store, jid, pid='115abc', state='unconfirmed',
                    deadline=time.time() - 600)

    assert engine.emby.repair_share_identities(time.time()) == 1
    row = store.db.execute('SELECT * FROM emby_deliveries WHERE job_id=?', (jid,)).fetchone()
    assert row['state'] == 'accepted' and row['attempts'] >= 1
    assert row['next_due'] <= time.time() and row['deadline'] > time.time()
    payload = json.loads(row['payload'])
    assert payload['targets'][0]['pid'] == 'share/s1'
    # 只修一次：再调用不重复改动。
    assert engine.emby.repair_share_identities(time.time()) == 0
