"""Cookie management and durable account-isolated directory selections."""

import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from dev.tests.test_cloud_workbench import cloud
from app import main, readiness
from app.cloud import accounts, directories, credentials, store, provider115
from app.cloud.errors import CloudError


@pytest.fixture
def client(cloud, monkeypatch):
    from app.routers import deps
    monkeypatch.setattr(readiness, "failed", lambda: False)
    instance = TestClient(main.app)
    instance.cookies.set(deps.SESSION_COOKIE, deps._sign_session(deps.WEB_USER, int(time.time()+3600)))
    instance.headers["Origin"] = "http://testserver"
    monkeypatch.setattr(accounts, "Provider115", lambda *a, **kw: cloud.remote)
    monkeypatch.setattr(directories, "for_account", lambda aid: cloud.remote)
    return instance


def test_cookie_import_is_verified_encrypted_and_not_returned(cloud, client):
    cookie = "UID=123_H1_1; CID=confidential-cid; SEID=confidential-seid"
    result = client.post('/api/cloud/accounts/cookie', json={"cookie": cookie, "account_id": cloud.id})
    assert result.status_code == 200 and result.json()['account_id'] == cloud.id
    assert 'confidential' not in result.text
    assert 'confidential' not in store.account(cloud.id)['sealed']
    assert credentials.load(cloud.id)[1]['cookie'] == cookie
    status = client.get('/api/cloud/status').json()
    assert 'confidential' not in str(status)
    account = next(i for i in status['accounts'] if i['id'] == cloud.id)
    assert account['authorization']['device'] == 'ipad'
    assert account['authorization']['cookie_delete']
    assert any(i['id'] == 'ipad' for i in status['devices'])


@pytest.mark.parametrize('cookie',[None,'UID=1; SEID=secret','UID=1; CID=secret; SEID=secret\nHeader: value','secret'*3000])
def test_invalid_cookie_never_echoes_secret(cookie, client):
    result = client.post('/api/cloud/accounts/cookie',json={'cookie':cookie})
    assert result.status_code == 400 and 'secret' not in result.text


def test_wrong_uid_cannot_replace_selected_account(cloud, client):
    other = accounts.save_authorization('999',{}, {'cookie':'UID=999_H1_1; CID=x; SEID=y'})
    result = client.post('/api/cloud/accounts/cookie',json={'cookie':'UID=123_H1_1; CID=x; SEID=y','account_id':other})
    assert result.status_code == 409
    assert credentials.load(other)[1]['cookie'].startswith('UID=999_')


def test_failed_profile_keeps_existing_cookie(cloud,client,monkeypatch):
    before=store.account(cloud.id)['sealed']
    monkeypatch.setattr(cloud.remote,'profile',Mock(side_effect=CloudError('检测失败',502)))
    assert client.post('/api/cloud/accounts/cookie',json={'cookie':'UID=123_H1_1; CID=x; SEID=y','account_id':cloud.id}).status_code==502
    assert store.account(cloud.id)['sealed']==before


def test_directories_persist_and_do_not_mix_accounts(cloud,client):
    endpoint='/api/cloud/accounts/'+cloud.id+'/directories'
    result=client.post(endpoint,json={'directories':{'source':'1','target':'2','transfer':'3'},'expected_revision':0})
    assert result.status_code==200
    values=result.json()
    assert values['directories']['source']=={'id':'1','name':'来源'}
    assert values['revision']==1
    assert client.get(endpoint).json()['directories']==values['directories']
    other=accounts.save_authorization('999',{}, {'cookie':'UID=999_H1_1; CID=x; SEID=y'})
    assert directories.get(other)['directories']['source'] is None
    stale=client.post(endpoint,json={'directories':{'unknown':'1'},'expected_revision':0})
    assert stale.status_code==409 and directories.get(cloud.id)['revision']==1


def test_file_or_same_target_is_rejected(cloud,client):
    endpoint='/api/cloud/accounts/'+cloud.id+'/directories'
    for patch in [{'source':'4'},{'source':'1','target':'1'},{'unexpected':'1'}]:
        result=client.post(endpoint,json={'directories':patch,'expected_revision':0})
        assert result.status_code in (400,409)
    assert directories.get(cloud.id)['revision']==0


def test_account_change_during_directory_check_prevents_save(cloud,client,monkeypatch):
    original=cloud.remote.info
    def changed(cid):
        with store.db() as con:
            con.execute('UPDATE cloud_accounts SET revision=revision+1 WHERE id=?',(cloud.id,))
        return original(cid)
    monkeypatch.setattr(cloud.remote,'info',changed)
    result=client.post('/api/cloud/accounts/'+cloud.id+'/directories',json={'directories':{'source':'1'},'expected_revision':0})
    assert result.status_code==409 and directories.get(cloud.id)['revision']==0


def test_cookie_delete_uses_cookie_payload_and_exact_absence_evidence():
    provider=object.__new__(provider115.Provider115)
    provider.call=Mock(return_value={'state':True})
    provider.delete('12345678901234567890')
    provider.call.assert_called_once_with('fs_delete',{'fid':'12345678901234567890'},write=True)
    provider.client=SimpleNamespace(fs_file=lambda *a,**k:{'state':False,'errno':20018})
    provider.list=Mock(return_value=[])
    source={'parent_id':'1','name':'video.mkv','size':100,'sha1':'A'*40}
    assert provider.removed_receipt('4',source,True)['parent_complete_absent']
    provider.client.fs_file=lambda *a,**k:{'state':False,'errno':40140125}
    with pytest.raises(CloudError):provider.removed_receipt('4',source,True)


def test_timeout_receipt_needs_original_file_id_not_just_same_name():
    provider=object.__new__(provider115.Provider115)
    provider.client=SimpleNamespace(fs_file=lambda *a,**k:{'state':False,'errno':20018})
    provider.list=Mock(return_value=[])
    provider.call=Mock(return_value={'state':True,'count':1,'data':[{'id':'4','name':'video.mkv','size':100}]})
    source={'parent_id':'1','name':'video.mkv','size':100,'sha1':'A'*40}
    assert provider.removed_receipt('4',source,False) is None
    provider.call.return_value={'state':True,'count':1,'data':[{'rid':'77','file_id':'4','name':'video.mkv','size':100,'sha1':'A'*40}]}
    assert provider.removed_receipt('4',source,False)['recycle_id']=='77'


def test_manual_cookie_endpoint_requires_existing_admin_auth():
    assert TestClient(main.app).post('/api/cloud/accounts/cookie',json={'cookie':'UID=123_H1_1; CID=x; SEID=y'}).status_code==401
