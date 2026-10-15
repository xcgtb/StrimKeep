"""Actual queue/SQLite/share receipts, unified log cursor and explicit link intake."""
import json
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app import runtime_logs
from app.cloud import events, store, jobs, workflow, directories, settings
from app.cloud.provider115 import parse_share
from app.cloud.errors import CloudError


@pytest.fixture
def intake(cloud,monkeypatch):
    monkeypatch.setattr(directories,'for_account',lambda aid:cloud.remote)
    directories.save(cloud.id,{'source':'1','target':'2','transfer':'3'},0)
    return cloud


def submit(cloud,request_id='a'*32,**patch):
    return workflow.transfer_link(cloud.id,patch.get('link','https://115.com/s/abcdef123'),patch.get('password','abcd'),patch.get('organize',False),patch.get('revision',1),request_id,patch.get('confirmed',True))


def logs(source='cloud',query=''):
    return runtime_logs.read(limit=500,source=source,query=query)['logs']


@pytest.mark.parametrize('value,password',[
 ('https://115.com/s/abcdef123?password=abcd','abcd'),
 ('我用115分享了文件 https://115cdn.com/s/abcdef123 提取码：abcd','abcd'),
 ('【资源】\nhttps://115.com/s/abcdef123-abcd\n复制后打开115','abcd'),
 ('https://115.com/s/abcdef123',''),
])
def test_mobile_copied_share_text(value,password):
    assert parse_share(value)==('abcdef123',password)


@pytest.mark.parametrize('value',['https://evil.test/s/abcdef123','https://115.com.evil.test/s/abcdef123','https://115.com/s/abcdef123 https://115.com/s/abcdef124','https://u:p@115.com/s/abcdef123'])
def test_share_parser_does_not_accept_arbitrary_hosts_or_multiple_shares(value):
    with pytest.raises(CloudError):parse_share(value)


def test_whole_link_queues_once_and_transfer_result_is_verified(intake):
    first=submit(intake);second=submit(intake)
    assert first==second and not intake.remote.writes
    jobs.execute(first)
    assert store.job(first)['state']=='success'
    assert len([r for r in intake.remote.writes if r[0]=='receive'])==1
    assert submit(intake)==first
    messages='\n'.join(r['message'] for r in logs())
    assert '转存已核实' in messages and '任务已排队' in messages and '开始执行' in messages
    assert first[:12] in messages and 'abcdef123' not in messages and 'abcd' not in messages


def test_direct_intent_secret_is_encrypted_and_cannot_change(intake):
    submit(intake)
    with store.db() as con:raw=con.execute('SELECT sealed FROM cloud_transfer_requests').fetchone()[0]
    assert 'abcd' not in raw and 'abcdef123' not in raw
    with pytest.raises(CloudError,match='其他内容'):submit(intake,password='new')


def test_revision_change_during_share_read_does_not_queue(intake,monkeypatch):
    original=intake.remote.share_page
    def page(*args):
        result=original(*args)
        directories.save(intake.id,{'transfer':'1'},1)
        return result
    monkeypatch.setattr(intake.remote,'share_page',page)
    with pytest.raises(CloudError,match='读取期间变化'):submit(intake)
    with store.db() as con:assert con.execute('SELECT COUNT(*) FROM cloud_jobs').fetchone()[0]==0
    assert not intake.remote.writes


@pytest.mark.parametrize('mode',['empty','paged'])
def test_empty_or_large_share_requires_browser_selection(intake,monkeypatch,mode):
    monkeypatch.setattr(intake.remote,'share_page',lambda *a:{'items':[] if mode=='empty' else [intake.remote.info('4')],'has_more':mode=='paged','total':0 if mode=='empty' else 101})
    with pytest.raises(CloudError,match='分批选择'):submit(intake)
    assert not intake.remote.writes


def test_confirmation_and_directory_revision_required(intake):
    with pytest.raises(CloudError):submit(intake,confirmed=False)
    with pytest.raises(CloudError):submit(intake,revision=0)
    assert not intake.remote.writes


def test_transfer_unknown_receipt_never_claims_success_or_replays(intake):
    intake.remote.receive_uncertain=True
    jid=submit(intake);jobs.execute(jid)
    assert store.job(jid)['state']=='uncertain'
    assert submit(intake)==jid
    messages='\n'.join(r['message'] for r in logs())
    assert '转存已核实' not in messages and 'uncertain' in messages
    assert len([r for r in intake.remote.writes if r[0]=='receive'])==1


def test_atomic_enqueued_log_rolls_back_with_job(intake):
    before=len(logs())
    with pytest.raises(RuntimeError):
        with store.db() as con:
            jobs.enqueue('sync',{'account_id':intake.id},con)
            raise RuntimeError('rollback')
    assert len(logs())==before
    with store.db() as con:assert con.execute('SELECT COUNT(*) FROM cloud_jobs').fetchone()[0]==0


def test_progress_emits_only_changed_phase_item_and_count(intake):
    jid=jobs.enqueue('sync',{})
    jobs.progress(jid,10,0,'识别','Movie.mkv')
    count=len(logs())
    jobs.progress(jid,10,0,'识别','Movie.mkv')
    assert len(logs())==count
    jobs.progress(jid,10,1,'识别','Movie.mkv')
    assert len(logs())==count+1


def test_organize_and_strm_report_each_stage(intake):
    plan=workflow.plan(intake.id,'1','2')
    jid=workflow.apply(plan['id'],True,True);jobs.execute(jid)
    assert store.job(jid)['state']=='success'
    sources={r['source'] for r in logs()}
    assert {'cloud_recognize','cloud_naming','cloud_organize','cloud_strm'}<=sources
    assert any('STRM 已发布' in r['message'] for r in logs())


def test_log_filters_advance_cursor_across_other_modules_without_losing_batches(intake):
    for i in range(5):events.emit('transfer','文件 '+str(i))
    events.emit('emby','等待入库')
    first=runtime_logs.read(limit=2,source='cloud_transfer')
    second=runtime_logs.read(after=first['cursor'],limit=2,source='cloud_transfer')
    assert len(first['logs'])==2 and len(second['logs'])==0  # Initial load returns latest two.
    assert second['cursor']>first['cursor']
    assert len(logs(query='文件'))==5
    assert len(logs(source='cloud_emby'))==1
    assert not logs(source="' OR 1=1 --")


def test_sensitive_log_values_are_redacted_and_bounded(intake):
    events.emit('transfer','Cookie=UID=secret; SEID=secret2 https://115.com/s/abcdef123?password=abcd 提取码:abcd token=tokSECRET')
    text=logs()[-1]['message']
    for secret in ['secret','secret2','abcdef123','abcd','tokSECRET']:assert secret not in text
    events.emit('strm','a'*10000)
    assert len(logs()[-1]['message'])<=2000


def test_api_direct_receive_is_idempotent_and_visible_in_log_center(intake,client):
    body={'account_id':intake.id,'link':'https://115.com/s/abcdef123','expected_revision':1,'request_id':'b'*32,'confirmed':True}
    response=client.post('/api/cloud/shares/receive',json=body)
    assert response.status_code==202
    assert client.post('/api/cloud/shares/receive',json=body).json()['job_id']==response.json()['job_id']
    view=client.get('/api/logs?source=cloud&query=任务已排队').json()
    assert len(view['logs'])==1
    assert view['logs'][0]['source']=='cloud_queue'
