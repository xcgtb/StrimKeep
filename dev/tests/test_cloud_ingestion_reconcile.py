"""Recover local receipt gaps without replaying publication, transfer or TG sends."""
import json
from unittest.mock import Mock
import pytest
from app.cloud import emby_library as emby, settings, store, services, events
from app import tg
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_linkage import asset, connection


def test_unconfigured_generated_strm_has_an_explanation_without_remote_io(cloud, monkeypatch):
    published=asset(cloud)
    remote=Mock(side_effect=AssertionError('listing must not access Emby'))
    monkeypatch.setattr(emby,'request',remote)
    records=emby.records(cloud.id)
    assert len(records)==1 and records[0]['asset_id']==published['id']
    assert records[0]['state']=='unverified' and not records[0]['id']
    assert '尚未启用独立' in records[0]['result']['message']
    settings.save({'emby_refresh_enabled':False})
    assert '全局' in emby.records(cloud.id)[0]['result']['message']
    remote.assert_not_called()
    with store.db() as con:assert not con.execute('SELECT 1 FROM cloud_ingestion').fetchone()


def test_film_already_in_emby_backfills_confirms_without_refresh_or_notification(cloud, monkeypatch):
    published=asset(cloud)  # Published before any independent connection existed.
    assert emby.records(cloud.id)[0]['state']=='unverified'
    connection(cloud)
    calls=[]
    def request(c,path,**kwargs):
        calls.append((path,kwargs))
        assert path=='/Items', 'existing exact match needs no update request'
        return {'Items':[{'Id':'film55','Type':'Movie','Path':kwargs['params']['Path'],'ProviderIds':{'Tmdb':'44'}}]}
    monkeypatch.setattr(emby,'request',request)
    sender=Mock(side_effect=AssertionError('receipt must not notify again'))
    monkeypatch.setattr(tg,'notify_telegram_card',sender)
    monkeypatch.setattr(tg,'notify_telegram',sender)
    records=emby.records(cloud.id)
    assert len(records)==1 and records[0]['state']=='pending' and not calls
    # Page reads under another job context cannot create duplicate receipts.
    token=events._JOB.set('another-job-context')
    try:assert len(emby.records(cloud.id))==1
    finally:events._JOB.reset(token)
    emby.poll_one()
    records=emby.records(cloud.id)
    assert records[0]['state']=='confirmed' and records[0]['result']['emby_item_id']=='film55'
    assert len(calls)==1
    sender.assert_not_called()
    settings.save({'emby_refresh_enabled':False})
    assert len(emby.records(cloud.id))==1 and emby.records(cloud.id)[0]['state']=='confirmed'


def test_share_needs_its_own_mapping_then_registers_without_regenerating(cloud):
    published=asset(cloud)
    published['payload']['source_kind']='share'
    with store.db() as con:con.execute('UPDATE cloud_assets SET payload=? WHERE id=?',(store.encode(published['payload']),published['id']))
    cfg=connection(cloud,share_root='')
    records=emby.records(cloud.id)
    assert records[0]['state']=='unverified' and '路径映射' in records[0]['result']['message']
    emby.save(cloud.id,{'expected_revision':cfg['revision'],'connection':{'id':cfg['items'][0]['id'],'share_root':'/share'}})
    record=emby.records(cloud.id)[0]
    assert record['state']=='pending' and record['path'].startswith('/share/')
    assert store.asset(published['id'])['payload']['digest']==published['payload']['digest']


def test_read_does_not_restart_timed_out_checks_or_show_revoked_assets(cloud):
    connection(cloud);published=asset(cloud)
    with store.db() as con:con.execute("UPDATE cloud_ingestion SET state='timeout',attempts=120")
    for _ in range(2):assert emby.records(cloud.id)[0]['state']=='timeout'
    with store.db() as con:
        assert con.execute('SELECT COUNT(*) FROM cloud_ingestion').fetchone()[0]==1
        con.execute("UPDATE cloud_assets SET state='revoked' WHERE id=?",(published['id'],))
    assert emby.records(cloud.id)==[]


def test_disabled_connection_does_not_hide_generated_asset(cloud):
    connection(cloud,enabled=False);asset(cloud)
    assert emby.records(cloud.id)[0]['state']=='unverified'
    settings.save({'emby_refresh_enabled':False})
    cfg=services.get(cloud.id,'emby_library',emby.DEFAULT)
    emby.save(cloud.id,{'expected_revision':cfg['revision'],'connection':{'id':cfg['config']['connections'][0]['id'],'enabled':True}})
    assert '全局' in emby.records(cloud.id)[0]['result']['message']
    settings.save({'emby_refresh_enabled':True})
    assert emby.records(cloud.id)[0]['state']=='pending'


def test_recent_account_scope_is_bounded_before_reconciliation(cloud):
    published=asset(cloud)
    with store.db() as con:
        source=con.execute('SELECT * FROM cloud_assets WHERE id=?',(published['id'],)).fetchone()
        for index in range(105):
            row=list(source);row[0]='asset-'+str(index);row[2]='file-'+str(index);row[3]+='.'+str(index);row[-1]=index+10000000000
            con.execute('INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?)',row)
        row=list(source);row[0]='foreign';row[1]='another-account';row[3]+='.foreign';row[-1]=99999999999
        con.execute('INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?)',row)
    result=emby.records(cloud.id)
    assert len(result)==100 and result[0]['asset_id']=='asset-104'
    assert not any(r['asset_id']=='foreign' for r in result)
    assert 'asset-0' not in {r['asset_id'] for r in result}
