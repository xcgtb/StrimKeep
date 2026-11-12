import asyncio
import json

import httpx
import pytest

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.store import Store
from test_organize import CONFIG, new_job
from test_upgrade import seed, policy


@pytest.mark.parametrize('shape', ['list','nested-map','flat-map','empty-flat'])
def test_recycle_response_shapes_keep_original_id_and_recycle_id_distinct(tmp_path, shape):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        entry = {'id': '456789', 'file_id': '3546250284495799509', 'file_name': '片名[\x02128074]1080p.mkv', 'file_size': '15177603331'}
        payload = {'state': True, 'count': '1', 'offset': 0}
        if shape == 'list':
            payload['data'] = [{**entry, 'rid': '456789'}]
        elif shape == 'nested-map':
            payload['data'] = {'456789': entry}
        elif shape == 'flat-map':
            payload['456789'] = entry
        else:
            payload.update(count='0', error='', is_asc=0, order='dtime', page_size=100, rb_pass=0)
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
        rows = await cloud.recycle_files()
        if shape == 'empty-flat':
            assert rows == []
        else:
            assert rows == [{'id': entry['file_id'], 'rid': '456789', 'name': '片名👊1080p.mkv', 'size': 15177603331}]
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_real_nas_record_id_is_not_the_original_file_id(tmp_path):
    from strimkeep.recycle import match_record
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie':'UID=1_A1; CID=test; SEID=test'})
        cloud = Cloud115(store); await cloud.http.aclose()
        name = '海底总动员 (2003) - 1080p.BLURAY.H.264.8-bit.23.976fps.TrueHD 7.1.mkv'
        data = {'state':True, 'count':'1', 'offset':0, 'data':[
            {'id':'3546310263571482388', 'file_name':name, 'file_size':'15177603331',
             'cid':'354630921883910167', 'type':'1', 'dtime':'1792769126'}]}
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=data)))
        entries = await cloud.recycle_files()
        source = {'id':'3546307877859755390','name':name,'size':15177603331,'parent_id':'354630921883910167'}
        assert entries[0]['id'] == '' and entries[0]['rid'] == '3546310263571482388'
        assert match_record(source, {'at':1792769125.5, 'before_rids':[]}, entries)['rid'] == entries[0]['rid']
        assert match_record(source, {'at':1792769125.5}, entries)['match_kind'] == 'legacy_intent_time'
        assert match_record(source, {'at':1792769125.5, 'before_rids':[entries[0]['rid']]}, entries) is None
        assert match_record(source, {'at':1792769127}, entries) is None
        assert match_record({**source,'parent_id':'300'}, {'at':1792769125.5, 'before_rids':[]}, entries) is None
        assert match_record(source, {'at':1792769125.5}, [{**entries[0], 'deleted_at':None}]) is None
        assert match_record(source, {'at':1792769125.5, 'before_rids':[]}, [{**entries[0], 'is_dir':True}]) is None
        with pytest.raises(CloudError):
            match_record(source, {'at':1792769125.5, 'before_rids':[]}, [*entries, {**entries[0],'rid':'999'}])
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_live_file_check_distinguishes_real_sdk_missing_error_from_transport_failure(tmp_path):
    async def scenario():
        store=Store(tmp_path,'admin','test-password-1234')
        store.set_many({'cookie':'UID=1_A1; CID=test; SEID=test'})
        cloud=Cloud115(store);await cloud.http.aclose()
        cloud.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
            'state':False,'errno':20018,'error':'文件不存在或已删除。'})))
        assert await cloud.live_file_exists('1000') is False
        def failed(request):raise httpx.ConnectError('offline')
        cloud.http._transport=httpx.MockTransport(failed);cloud.next_request=0
        with pytest.raises(CloudError) as exc:await cloud.live_file_exists('1000')
        assert exc.value.code=='CLOUD_UNAVAILABLE'
        await cloud.close();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('legacy', [False, True])
def test_different_recycle_id_finishes_pipeline_and_resumes_legacy_intent(tmp_path, legacy):
    import time
    async def scenario():
        store, cloud, http, engine, requests, _ = await seed(tmp_path)
        adapter = Cloud115(store)
        adapter.exact_number = Cloud115.exact_number
        async def call(*args, **kwargs):
            return {'state':True,'offset':0,'count':str(len(cloud.bin)), 'data':[
                {'id': e['rid'], 'file_name':e['name'], 'file_size':str(e['size']),
                 'cid':old_parent, 'type':'1', 'dtime':str(int(time.time()))}
                for e in cloud.bin.values()]}
        adapter.call = call
        cloud.recycle_files = adapter.recycle_files
        old_parent = cloud.nodes['1000']['parent_id']
        cloud.add_video('2000')['size'] = 2000
        jid = new_job(store, {**CONFIG,'upgrade_policy':policy('size')})
        if legacy:
            def interrupted(): raise asyncio.CancelledError()
            cloud.after_recycle = interrupted
            with pytest.raises(asyncio.CancelledError): await engine.run_job(jid)
            row = store.db.execute('SELECT id,plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()
            plan = json.loads(row['plan'])
            plan['wash']['actions']['1000'].pop('before_rids')
            engine.update_item(row['id'], plan=plan)
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed', dict(engine.job(jid))
        row = store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        action = json.loads(row['plan'])['wash']['actions']['1000']
        assert action['rid']=='91000' and action['source']['id']=='1000'
        assert action['observed']['match_kind']==('legacy_intent_time' if legacy else 'new_recycle_record')
        assert sum(w[:2]==('recycle','1000') for w in cloud.writes)==1
        assert '.sknew-' not in cloud.nodes['2000']['name']
        assert len(list((tmp_path/'strm').rglob('*.strm')))==1
        await adapter.close(); await http.aclose(); store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bad', ['missing-offset','fraction-size','bool-count','null-data','unknown-entry','nonempty-count-empty'])
def test_unknown_recycle_data_is_not_treated_as_empty_or_success(tmp_path, bad):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
        cloud = Cloud115(store); await cloud.http.aclose()
        data = {'state': True, 'count': 1, 'offset': 0,
                'data': [{'id': '1000', 'rid': '456789', 'file_name': 'movie.mkv', 'file_size': '1000'}]}
        if bad == 'missing-offset': data.pop('offset')
        elif bad == 'fraction-size': data['data'][0]['file_size'] = 1000.5
        elif bad == 'bool-count': data['count'] = True
        elif bad == 'null-data': data['data'] = None
        elif bad == 'unknown-entry': data.pop('data'); data['files'] = []
        else: data.pop('data')
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)))
        with pytest.raises(CloudError) as exc:
            await cloud.recycle_files()
        assert exc.value.code == 'RECYCLE_UNVERIFIED'
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_confirmed_recycle_survives_later_external_purge_without_recycling_again(tmp_path):
    async def scenario():
        store, cloud, http, engine, _, _ = await seed(tmp_path)
        cloud.add_video('2000')['size'] = 2000
        jid = new_job(store, {**CONFIG, 'upgrade_policy': policy('size')})
        save = engine.upgrader.save
        def pause_after_evidence(item_id, plan, step):
            save(item_id, plan, step)
            if step.startswith('回收结果已核实'):
                engine.update_job(jid, state='paused')
        engine.upgrader.save = pause_after_evidence
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'paused'
        row = store.db.execute('SELECT * FROM organize_items WHERE job_id=?', (jid,)).fetchone()
        action = json.loads(row['plan'])['wash']['actions']['1000']
        assert action['state'] == 'confirmed' and action['evidence']['kind'] == 'recycle'
        cloud.bin.clear()  # independent application removes already confirmed evidence
        engine.upgrader.save = save
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        assert len([w for w in cloud.writes if w[:2] == ('recycle','1000')]) == 1
        assert '.sknew-' not in cloud.nodes['2000']['name']
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_confirmed_recycle_does_not_ignore_a_restored_file(tmp_path):
    async def scenario():
        store, cloud, http, engine, _, _ = await seed(tmp_path)
        old = cloud.nodes['1000'].copy()
        cloud.add_video('2000')['size'] = 2000
        jid = new_job(store, {**CONFIG, 'upgrade_policy': policy('size')})
        save = engine.upgrader.save
        def pause(item_id, plan, step):
            save(item_id, plan, step)
            if step.startswith('回收结果已核实'): engine.update_job(jid, state='paused')
        engine.upgrader.save = pause
        await engine.run_job(jid)
        cloud.bin.clear(); cloud.nodes['1000'] = {**old, 'parent_id': '300'}
        engine.upgrader.save = save
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial'
        assert len([w for w in cloud.writes if w[:2] == ('recycle','1000')]) == 1
        assert cloud.nodes['1000']['parent_id'] == '300'
        await http.aclose(); store.close()
    asyncio.run(scenario())
