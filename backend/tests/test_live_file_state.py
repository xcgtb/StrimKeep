"""Exercise live-state checks through the pinned SDK and real HTTP transport contract."""
import asyncio
import json
import time

import httpx
import pytest

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.store import Store
from test_organize import CONFIG, MEDIA, new_job
from test_upgrade import policy, upgraded_setup


OLD_ID = '3546307877859755390'
NAS_SKIM = {'state': True, 'errNo': 0, 'error': '', 'data': [{
    'file_id': OLD_ID,
    'file_name': '海底总动员 (2003) - 1080p.BLURAY.H.264.8-bit.23.976fps.TrueHD 7.1.mkv',
    'file_size': '15177603331', 'pick_code': 'test-only', 'sha1': 'A' * 40,
}]}


def test_recycled_file_still_has_skim_metadata_but_get_info_returns_real_nas_code(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        requests = []
        def respond(request):
            requests.append(request.url.path)
            assert request.method == 'GET' and request.url.params['file_id'] == OLD_ID
            payload = NAS_SKIM if request.url.path == '/files/file' else {'state': False, 'code': 20018}
            return httpx.Response(200, json=payload)
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert await cloud.live_file_exists(OLD_ID) is False
            assert requests == ['/files/get_info']
        finally:
            await cloud.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('row,expected', [
    ({'fid': OLD_ID, 'cid': '300', 'aid': '1'}, True),
    ({'cid': OLD_ID, 'pid': '300', 'aid': 1}, True),
    ({'file_id': OLD_ID, 'area_id': 1}, True),
    ({'fid': OLD_ID, 'aid': 12}, True),
    ({'fid': OLD_ID, 'aid': '7'}, False),
    ({'fid': OLD_ID, 'aid': '120'}, False),
])
def test_detail_state_covers_restored_moved_file_directory_and_retired_areas(tmp_path, row, expected):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        def respond(request):
            assert request.url.path == '/files/get_info'
            return httpx.Response(200, json={'state': True, 'data': [row]})
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert await cloud.live_file_exists(OLD_ID) is expected
        finally:
            await cloud.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('rows', [
    None, [], ['invalid'], [{}, {}],
    [{'fid': '999', 'aid': 1}],
    [{'fid': OLD_ID}],
    [{'fid': OLD_ID, 'aid': None}],
    [{'fid': OLD_ID, 'aid': True}],
    [{'fid': OLD_ID, 'aid': 999}],
    [{'fid': OLD_ID, 'aid': 1, 'area_id': 7}],
    [{'fid': OLD_ID, 'file_id': '999', 'aid': 1}],
])
def test_incomplete_or_conflicting_detail_never_confirms_retirement(tmp_path, rows):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'state': True, 'data': rows})))
        try:
            with pytest.raises(CloudError) as exc:
                await cloud.live_file_exists(OLD_ID)
            assert exc.value.code == 'SCHEMA_CHANGED'
        finally:
            await cloud.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['network', 'login', 'unknown-code'])
def test_live_detail_failures_never_become_file_missing(tmp_path, failure):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        def respond(request):
            if failure == 'network':
                raise httpx.ConnectError('offline')
            if failure == 'login':
                return httpx.Response(403)
            return httpx.Response(200, json={'state': False, 'code': 987654})
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(CloudError) as exc:
                await cloud.live_file_exists(OLD_ID)
            assert exc.value.code != 'FILE_NOT_FOUND'
        finally:
            await cloud.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('resume', ['new', 'legacy-intent', 'confirmed-purged', 'restored-moved'])
def test_pipeline_uses_detail_state_for_recycle_resume_and_output_cleanup(tmp_path, resume):
    async def scenario():
        store, cloud, http, engine, _, media = upgraded_setup(tmp_path)
        cloud.add_video('1000')
        media['1000'] = {**MEDIA, 'width': 1920, 'height': 1080,
                         'codec': 'h264', 'bit_depth': 8, 'dynamic_range': 'SDR'}
        baseline = new_job(store)
        await engine.run_job(baseline)
        assert engine.job(baseline)['state'] == 'completed'
        store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
        old = cloud.nodes['1000'].copy()
        cloud.add_video('2000')['size'] = 2000
        adapter = Cloud115(store)
        await adapter.http.aclose()
        reads = []
        def respond(request):
            if request.url.path == '/rb':
                # Web /rb returns a new record ID, never the original file ID.
                return httpx.Response(200, json={
                    'state': True, 'offset': 0, 'count': str(len(cloud.bin)),
                    'data': [{'id': entry['rid'], 'file_name': entry['name'],
                              'file_size': str(entry['size']), 'cid': old['parent_id'],
                              'type': '1', 'dtime': str(int(time.time()))}
                             for entry in cloud.bin.values()]})
            fid = request.url.params['file_id']
            reads.append((request.url.path, fid))
            if request.url.path == '/files/file':
                # Real skim semantics: retired metadata remains readable without aid.
                return httpx.Response(200, json={'state': True, 'data': [
                    {'file_id': fid, 'file_name': old['name'], 'file_size': str(old['size'])}]})
            assert request.url.path == '/files/get_info'
            if fid in cloud.nodes:
                node = cloud.nodes[fid]
                return httpx.Response(200, json={'state': True, 'data': [
                    {'fid': fid, 'cid': node['parent_id'], 'aid': 1}]})
            return httpx.Response(200, json={'state': False, 'code': 20018})
        adapter.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        cloud.live_file_exists = adapter.live_file_exists
        cloud.recycle_files = adapter.recycle_files
        jid = new_job(store, {**CONFIG, 'upgrade_policy': policy('resolution', 'size')})
        try:
            if resume == 'legacy-intent':
                def interrupt():
                    raise asyncio.CancelledError()
                cloud.after_recycle = interrupt
                with pytest.raises(asyncio.CancelledError):
                    await engine.run_job(jid)
                row = store.db.execute('SELECT id,plan FROM organize_items WHERE job_id=?', (jid,)).fetchone()
                plan = json.loads(row['plan'])
                plan['wash']['actions']['1000'].pop('before_rids')
                engine.update_item(row['id'], plan=plan)
            elif resume in ('confirmed-purged', 'restored-moved'):
                save = engine.upgrader.save
                def pause(item_id, plan, step):
                    save(item_id, plan, step)
                    if step.startswith('回收结果已核实'):
                        engine.update_job(jid, state='paused')
                engine.upgrader.save = pause
                await engine.run_job(jid)
                assert engine.job(jid)['state'] == 'paused'
                cloud.bin.clear()
                if resume == 'restored-moved':
                    cloud.nodes['1000'] = {**old, 'parent_id': '300'}
                engine.upgrader.save = save
            await engine.run_job(jid)
            assert sum(w[:2] == ('recycle', '1000') for w in cloud.writes) == 1
            assert reads and all(path == '/files/get_info' for path, _ in reads)
            if resume == 'restored-moved':
                assert engine.job(jid)['state'] == 'partial'
                assert cloud.nodes['1000']['parent_id'] == '300'
                assert len(list((tmp_path / 'strm').rglob('*.strm'))) == 2
            else:
                assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
                row = store.db.execute('SELECT plan FROM organize_items WHERE job_id=?', (jid,)).fetchone()
                action = json.loads(row['plan'])['wash']['actions']['1000']
                assert action['rid'] == '91000' and action['observed']['id'] == ''
                assert action['observed']['match_kind'] == (
                    'legacy_intent_time' if resume == 'legacy-intent' else 'new_recycle_record')
                assert '.sknew-' not in cloud.nodes['2000']['name']
                outputs = list((tmp_path / 'strm').rglob('*.strm'))
                assert len(outputs) == 1
                old_mapping = store.db.execute('SELECT revoked FROM playbacks WHERE file_id=?', ('1000',)).fetchone()
                assert old_mapping['revoked'] == 1
        finally:
            await adapter.close()
            await http.aclose()
            store.close()
    asyncio.run(scenario())
