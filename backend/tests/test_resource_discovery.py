import httpx
import pytest
from fastapi.testclient import TestClient
from strimkeep.main import create_app
from strimkeep.resource_discovery import clean_item, validate_base
from test_app import FakeCloud


def test_base_url_and_direct_link_safety():
    assert validate_base('http://192.0.2.10:12366/') == 'http://192.0.2.10:12366'
    for value in ('http://example.org', 'http://u:p@192.0.2.10', 'https://example.org/path', 'file:///tmp/x'):
        with pytest.raises(ValueError): validate_base(value)
    locked=clean_item({'title':'locked','is_unlocked':False,'points_known':False,'unlock_points':0,'sharer':'COKEMV'})
    assert locked['sharer']=='COKEMV'
    assert locked['share_url'] == '' and locked['unlock_points'] is None
    assert clean_item({'provider':'115','message_text':'https://115.com/s/abc123 提取码: abcd'})['share_url'] == 'https://115.com/s/abc123?password=abcd'
    assert clean_item({'provider':'115','message_text':'https://115.com/s/one https://115.com/s/two'})['share_url'] == ''
    assert clean_item({'provider':'123','message_text':'https://115.com/s/abc123'})['share_url'] == ''



def test_clean_item_syncs_remark_and_all_detail_fields():
    """影视探索的资源卡要靠备注判断「这个版本是什么」，字段必须整份同步过去。"""
    item = {
        'title': '流人 (2022)', 'provider': '115', 'source': 're0', 'slug': 'x1',
        'remark': 'S06 4K ATVP WEB-DL DV&HDR DDP5.1 内封简繁+简/繁英双语字幕[SCDIY@FGTWeb] 修复E01后续缺失字幕的问题',
        'message_text': 'S06 4K ATVP WEB-DL DV&HDR DDP5.1', 'published_at': '2026-09-16 15:55:50',
        'quality': 'WEB-DL', 'resolution': '2160p', 'size': '54.31GB', 'sharer': '山茶',
        'link_type': 'share', 'validate_status': 'valid', 'validate_message': '链接可用',
        'validation_label': '有效', 'validation_known': True, 'is_official': False,
        'dolby_vision': True, 'channel_title': '',
        'resolution_tags': ['4K'], 'subtitle_types': ['内封'], 'subtitle_languages': ['简中', '简英'],
        'source_tags': ['WEB-DL/WEBRip'], 'supported_targets': ['115'],
        'resource_specification': '2160p.Apple TV+.WEB-DL.DoVi HDR.DDP 5.1-SCDIY@FGTWeb',
        'resource_specs': {'resource_pix': '2160p', 'web_source': 'Apple TV+',
                           'resource_team': 'SCDIY@FGTWeb'},
        'episode': {'season_num': 6, 'episode_num': 1, 'end_episode_num': None,
                    'kind': 'episode_marker', 'confidence': 'high'},
        'unlocked_users_count': 249, 'unlocked_users_known': True,
        'is_unlocked': True, 'points_known': True, 'unlock_points': 0,
    }
    clean = clean_item(item)
    assert clean['remark'] == item['remark']
    assert clean['message_text'] == item['message_text']
    assert clean['published_at'] == '2026-09-16 15:55:50'
    assert clean['subtitle_types'] == ['内封'] and clean['resolution_tags'] == ['4K']
    assert clean['source_tags'] == ['WEB-DL/WEBRip'] and clean['supported_targets'] == ['115']
    assert clean['resource_specification'].endswith('-SCDIY@FGTWeb')
    assert clean['resource_specs']['resource_team'] == 'SCDIY@FGTWeb'
    assert clean['episode'] == {'season_num': 6, 'episode_num': 1,
                               'kind': 'episode_marker', 'confidence': 'high'}
    assert clean['unlocked_users_count'] == 249 and clean['unlocked_users_known'] is True
    assert clean['dolby_vision'] is True and clean['is_official'] is False
    assert clean['validation_label'] == '有效' and clean['validate_message'] == '链接可用'
    # 脏数据仍然要拦：超长裁剪、异常集号与人数丢弃
    dirty = clean_item({'remark': 'x' * 500, 'resource_specs': {'resource_pix': 'y' * 200},
                        'episode': {'season_num': -1, 'episode_num': 99999},
                        'unlocked_users_count': -3})
    assert len(dirty['remark']) == 300 and len(dirty['resource_specs']['resource_pix']) == 80
    assert dirty['episode'] == {} and dirty['unlocked_users_count'] is None

def test_config_search_and_auth_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin'); monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path/'data', tmp_path/'strm', FakeCloud)
    seen = []; state = {'redirect':False}
    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get('authorization')))
        if state['redirect']:
            return httpx.Response(302, headers={'Location':'https://external.invalid/receive'})
        if request.url.path.endswith('/copy-link'):
            return httpx.Response(200, json={'success':True,'data':{'share_url':'https://115.com/s/abc123?password=abcd'}})
        return httpx.Response(200, json={'success':True,'data':{'items':[{'title':'result', 'provider':'115', 'message_text':'https://115.com/s/abc123', 'is_unlocked':True}], 'errors':[{'source':'other'}]}})
    with TestClient(app) as client:
        assert client.get('/api/v1/explore/resource-config').status_code == 401
        login=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'})
        client.headers['X-CSRF-Token']=login.json()['csrf']
        cfg=client.put('/api/v1/explore/resource-config',json={'base_url':'http://127.0.0.1:12366','token':'secret-token'})
        assert cfg.status_code == 200 and cfg.json()['token_configured'] and 'secret-token' not in cfg.text
        previous=app.state.http
        app.state.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        body={'title':'Example','tmdb_id':42,'media_type':'movie','year':'2026','sources':['re0']}
        result=client.post('/api/v1/explore/resources',json=body)
        assert result.status_code == 200 and result.json()['partial'] is True
        assert result.json()['items'][0]['share_url']=='https://115.com/s/abc123'
        assert seen==[('POST','/api/media/resources/search','Bearer secret-token')]
        assert client.post('/api/v1/explore/resources',json={**body,'sources':['invalid']}).status_code==422
        resolved=client.post('/api/v1/explore/resources/resolve',json={'source':'re0','provider':'115','slug':'candidate-1'})
        assert resolved.status_code==200 and resolved.json()['share_url'].endswith('abc123?password=abcd')
        assert client.post('/api/v1/explore/resources/resolve',json={'source':'re0','provider':'other','slug':'candidate-1'}).status_code==422
        state['redirect']=True
        assert client.post('/api/v1/explore/resources',json=body).status_code==502
        assert len(seen)==3 and [path for _,path,_ in seen]==['/api/media/resources/search','/api/media/resources/copy-link','/api/media/resources/search']
        assert client.post('/api/v1/explore/resources/resolve',json={'source':'re0','provider':'115','slug':'candidate-1'}).status_code==502
        assert len(seen)==4
        changed=client.put('/api/v1/explore/resource-config',json={'base_url':'http://127.0.0.1:12367'})
        assert not changed.json()['token_configured']
        assert client.post('/api/v1/explore/resources',json=body).status_code==409
        import asyncio
        asyncio.run(app.state.http.aclose()); app.state.http=previous
