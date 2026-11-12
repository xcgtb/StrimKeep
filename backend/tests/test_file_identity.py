"""Exercise actual SDK wire parsing: skim responses do not carry parent_id."""
import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.organizer import Organizer, UncertainState
from strimkeep.store import Store
from test_organize import setup, new_job


def skim_row(node):
    return {'file_id': node['id'], 'file_name': node['name'],
            'pick_code': node['pickcode'], 'file_size': str(node['size']), 'sha1': node['sha1']}


def detail_row(node):
    return {'file_id': node['id'], 'file_name': node['name'], 'pick_code': node['pickcode'],
            'file_size': str(node['size']), 'file_sha1': node['sha1'], 'parent_id': node['parent_id']}


async def adapter(store, serve):
    cloud = Cloud115(store)
    await cloud.http.aclose()
    cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    actual_call = cloud.call
    async def no_wait(method, *args, **kwargs):
        cloud.next_request = 0
        return await actual_call(method, *args, **kwargs)
    cloud.call = no_wait
    return cloud


NODE = {'id': '3546036918246442711', 'name': '测试剧集 (2024) S01E03.mkv',
        'size': 6234567890, 'pickcode': 'testpickcode', 'sha1': 'A' * 40, 'parent_id': '12345'}


def test_skim_without_parent_fetches_real_parent_via_sdk(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=x; SEID=x'})
        paths = []
        def serve(request):
            paths.append(request.url.path)
            if request.url.path == '/files/file':
                assert request.url.params['file_id'] == NODE['id']
                return httpx.Response(200, json={'state': True, 'data': [skim_row(NODE)]})
            assert request.method == 'POST' and request.url.path == '/files/supervision'
            assert parse_qs(request.content.decode())['pickcode'] == [NODE['pickcode']]
            return httpx.Response(200, json={'state': True, 'data': detail_row(NODE)})
        cloud = await adapter(store, serve)
        result = await cloud.file(NODE['id'])
        assert result == NODE
        assert paths == ['/files/file', '/files/supervision']
        await cloud.close(); store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value,code', [
    ('parent_id', None, 'SCHEMA_CHANGED'), ('parent_id', 'invalid', 'SCHEMA_CHANGED'),
    ('file_size', None, 'SCHEMA_CHANGED'), ('file_size', '12', 'FILE_CHANGED'),
    ('file_id', '999', 'FILE_CHANGED'), ('file_name', 'externally renamed.mkv', 'FILE_CHANGED'),
    ('file_sha1', 'B' * 40, 'FILE_CHANGED'), ('file_sha1', '', 'INVALID_FILE'),
    ('pick_code', 'otherpickcode', 'FILE_CHANGED'),
])
def test_incomplete_or_changed_detail_is_never_trusted(tmp_path, field, value, code):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=x; SEID=x'})
        def serve(request):
            if request.url.path == '/files/file':
                return httpx.Response(200, json={'state': True, 'data': [skim_row(NODE)]})
            return httpx.Response(200, json={'state': True, 'data': {**detail_row(NODE), field: value}})
        cloud = await adapter(store, serve)
        with pytest.raises(CloudError) as exc:
            await cloud.file(NODE['id'])
        assert exc.value.code == code
        assert 'testpickcode' not in str(exc.value)
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_existing_paused_job_recovers_with_actual_sdk_reads_without_duplicate_writes(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        cloud.add_video('1001', '测试剧集 (2024) S01E04.mkv')
        def serve(request):
            if request.url.path == '/files/file':
                node = cloud.nodes[request.url.params['file_id']]
                return httpx.Response(200, json={'state': True, 'data': [skim_row(node)]})
            assert request.url.path == '/files/supervision'
            pc = parse_qs(request.content.decode())['pickcode'][0]
            node = next(n for n in cloud.nodes.values() if n.get('pickcode') == pc)
            return httpx.Response(200, json={'state': True, 'data': detail_row(node)})
        actual = await adapter(store, serve)
        async def legacy(fid):
            result = await actual.file(fid)
            result['parent_id'] = None  # v0.2.0 erroneously trusted skim's missing field.
            return result
        cloud.file = legacy
        jid = new_job(store)
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial'
        assert all('父目录' in r['message'] for r in store.db.execute('SELECT message FROM organize_items WHERE job_id=?',(jid,)))
        assert not any(w[0] in ('move', 'rename') for w in cloud.writes)
        category_writes = list(cloud.writes)
        assert store.db.execute("SELECT count(*) FROM events WHERE message LIKE '%父目录%'").fetchone()[0] >= 2
        cloud.file = actual.file
        jid = new_job(store)  # An ordinary new scan retries the unchanged source.
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        assert cloud.writes[:len(category_writes)] == category_writes
        assert len([w for w in cloud.writes if w[0] == 'move']) == 2
        assert len([w for w in cloud.writes if w[0] == 'rename']) == 2
        assert len(list((tmp_path / 'strm').rglob('*.strm'))) == 2
        assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 2
        assert store.db.execute('SELECT count(*) FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0] == 2
        await actual.close(); await http.aclose(); store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('field,value,label', [
    ('id', '999', '文件 ID'), ('size', 1, '大小'), ('name', 'changed.mkv', '名称'),
    ('parent_id', '999', '父目录'),
])
def test_real_external_changes_still_stop_with_specific_field(field, value, label):
    with pytest.raises(UncertainState, match=label):
        Organizer.check_source(NODE, {**NODE, field: value}, NODE['name'], NODE['parent_id'])


def test_unverifiable_file_detail_pauses_instead_of_quarantining(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video().copy()
        async def changed(fid):
            raise CloudError('两次文件信息读取结果不一致，请核查文件是否正在变化。', 'FILE_CHANGED', 409)
        cloud.file = changed
        jid = new_job(store)
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial'
        assert cloud.nodes['1000'] == video
        assert not any(w[0] in ('rename', 'move') for w in cloud.writes)
        assert json.loads(store.db.execute('SELECT plan FROM organize_items').fetchone()[0])['failure'] is True
        await http.aclose(); store.close()
    asyncio.run(scenario())
