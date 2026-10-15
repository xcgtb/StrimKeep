"""Evidence logs, read-only templates and opted-in seven-dimensional source cleanup."""
import copy
import sqlite3
from pathlib import Path
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_governance import bound, clean
from dev.tests.test_my_drive import client
from app import storage, runtime_logs, cloud_bridge, engine, governance as seven
from app.cloud import events, store, settings, naming, governance


def test_diagnostic_fields_are_persisted_filtered_and_redacted(cloud, client):
    jid='a'*32
    events.emit('governance','检查源文件',jid,level='WARNING',details={
        'file_id':'4','sha1':'A'*40,'elapsed_ms':1250,'reason':'token=SECRET',
        'cookie':'MUST_NOT_STORE','reply':{'authorization':'MUST_NOT_STORE'}})
    data=client.get('/api/logs',params={'source':'cloud_governance','level':'WARNING','task':jid[:12]}).json()
    assert len(data['logs']) == 1
    row=data['logs'][0]
    assert row['details']['job_id']==jid and row['details']['elapsed_ms']==1250
    assert 'SECRET' not in store.encode(row) and 'MUST_NOT_STORE' not in store.encode(row)
    assert runtime_logs.read(query='A'*40)['logs'][0]['id']==row['id']
    assert not runtime_logs.read(level='ERROR')['logs']
    assert '"file_id": "4"' in runtime_logs.text()
    assert client.get('/api/logs?task=not-valid').status_code==400
    assert client.get('/api/logs?level=INVALID').status_code==400


def test_historical_schema_and_task_messages_are_preserved(cloud):
    con=sqlite3.connect(':memory:')
    con.execute('CREATE TABLE runtime_logs(id INTEGER PRIMARY KEY,ts TEXT,level TEXT,source TEXT,message TEXT)')
    con.execute("INSERT INTO runtime_logs VALUES(1,'old','INFO','old','historical')")
    storage._ensure_log_details(con)
    storage._ensure_log_details(con)
    assert con.execute('SELECT message,details FROM runtime_logs').fetchone()==('historical','{}')
    with store.db() as db:
        db.execute("INSERT INTO runtime_logs(ts,level,source,message) VALUES('old','INFO','cloud_queue','[任务 bbbbbbbbbbbb] old')")
    assert runtime_logs.read(task='b'*12)['logs'][0]['message'].endswith('old')


@pytest.mark.parametrize('style',['standard','compact','template','original'])
def test_template_details_are_read_only_and_describe_actual_mode(cloud,client,style):
    before=settings.get()
    response=client.post('/api/cloud/naming/details',json={'settings':{'naming_style':style}})
    assert response.status_code==200
    result=response.json()['template']
    assert result['style']==style and len(result['items'])==2
    tv=result['items'][1]
    if style in ('standard','compact'):
        assert tv['name'].startswith('金色.2026.S01E01.')
        assert '{tmdb-123456}' in tv['directory']
        assert ('第一集' in tv['name']) == (style=='standard')
    if style=='original':assert tv['name']=='original.S01E01.mkv'
    assert settings.get()==before and not cloud.remote.writes


def test_rename_disabled_details_preserve_source_names(cloud):
    result=naming.describe({**settings.get(),'rename_enabled':False})
    assert result['tv_file']=='原文件名'
    assert result['items'][1]['name']=='original.S01E01.mkv'


def test_binding_record_mismatch_never_dispatches(bound):
    with store.db() as con:
        con.execute("UPDATE cloud_assets SET file_id='777' WHERE id=?",(bound.asset['id'],))
    result=clean(bound)
    assert result['errors'] and bound.path.exists() and not bound.deleted


def test_unselected_companion_reference_protects_shared_sidecar(bound):
    payload=copy.deepcopy(bound.asset['payload'])
    payload['source']={**payload['source'],'id':'99'}
    with store.db() as con:
        con.execute('INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?)',
                    (store.ident(),bound.cloud.id,'99',str(bound.cloud.root/'other.strm'),
                     store.encode(payload),'nonce','active',1))
    result=clean(bound)
    assert result['errors'] and not bound.deleted and bound.path.exists()


def test_deletion_evidence_distinguishes_remote_receipts_and_nas_cleanup(bound):
    result=clean(bound)
    assert result['strm_removed']==1
    logs=runtime_logs.read(source='cloud_governance',limit=100)['logs']
    assert any(r['details'].get('status')=='dispatched' for r in logs)
    assert any(r['details'].get('status')=='verified' and r['details'].get('sha1')=='A'*40 for r in logs)
    assert logs[-1]['details']['status']=='revoked'


def generated_roots(bound,monkeypatch):
    legacy=bound.cloud.root.parent/'legacy'
    share=bound.cloud.root.parent/'share-output'
    legacy.mkdir();share.mkdir()
    monkeypatch.setattr(engine,'L_ROOT',legacy)
    monkeypatch.setattr(engine,'S_ROOT',share)
    settings.save({'output_root':str(bound.cloud.root),'share_output_root':str(share),'governance_generated_enabled':True})
    return legacy,share


def test_generated_roots_opt_in_merges_without_mutating_cached_library(bound,monkeypatch):
    legacy,share=generated_roots(bound,monkeypatch)
    engine._invalidate_lib_cache()
    base=engine._get_lib(legacy)
    combined=cloud_bridge.governance_library(legacy)
    assert base.strm_count==0 and combined.strm_count==1
    assert any(bound.path in paths for paths in combined.mov.values())
    action=type('Act',(),{'files':[bound.path],'guard_paths':[],'kind':'loc','meta':{}})()
    snapshots,guards=seven._execution_snapshots([action])
    assert snapshots[str(bound.path)]['root']==str(bound.cloud.root)
    assert guards[0]['cloud_sources'][str(bound.path)]['file_id']=='4'
    result=cloud_bridge.governance_delete([bound.path],True)
    assert result['strm_removed']==1 and set(bound.deleted)=={'4','5'}


def test_opt_out_or_missing_generated_root_never_expands_scope(bound,monkeypatch):
    legacy,share=generated_roots(bound,monkeypatch)
    settings.save({'governance_generated_enabled':False})
    assert cloud_bridge.governance_roots(True)==(legacy,)
    with pytest.raises(ValueError):cloud_bridge.governance_root(bound.path,True)
    settings.save({'governance_generated_enabled':True,'output_root':str(legacy.parent/'missing')})
    with pytest.raises(ValueError,match='不能当作空库'):cloud_bridge.governance_roots(True)


def test_generated_scope_and_output_changes_invalidate_rule_snapshot(bound,monkeypatch):
    before=seven._current_rule_snapshot()['sig']
    generated_roots(bound,monkeypatch)
    assert seven._current_rule_snapshot()['sig'] != before


def test_foreign_reference_in_extra_root_never_uses_cd2_fallback(bound,monkeypatch):
    generated_roots(bound,monkeypatch)
    foreign=bound.cloud.root/'foreign.strm';foreign.write_text('http://upstream/file')
    assert cloud_bridge.preview(foreign,bound.cloud.root,True)['blocked']
    with pytest.raises(ValueError,match='未登记引用'):
        cloud_bridge.governance_delete([foreign],True)
    assert foreign.exists() and not bound.deleted


def test_generated_root_cannot_cross_into_other_legacy_library(bound,monkeypatch):
    legacy,share=generated_roots(bound,monkeypatch)
    inside=share/'wrong-local';inside.mkdir()
    settings.save({'output_root':str(inside),'share_output_root':str(legacy.parent/'separate-share')})
    with pytest.raises(ValueError,match='另一类原治理库'):
        cloud_bridge.governance_roots(True)
