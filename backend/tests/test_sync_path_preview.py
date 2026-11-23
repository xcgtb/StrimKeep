from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from strimkeep.phase5_api import install_phase5
from strimkeep.sync import Sync


@pytest.mark.parametrize(('root', 'name', 'status', 'skipped', 'target', 'mount'), [
    ('200', '测试片.mkv', 200, False, 'main', '/library'),
    ('200', '测试片.mkv', 200, False, 'secondary', '/secondary'),
    ('200', '测试片.mkv', 200, False, 'off', None),
    ('200', '测试片.nfo', 200, True, 'main', '/library'),
    ('999', '测试片.mkv', 409, False, 'main', '/library'),
])
def test_path_preview_matches_sync_without_writes(tmp_path, root, name, status, skipped, target, mount):
    app = FastAPI()
    trail = [{'id': '0', 'name': ''}, {'id': '100', 'name': '媒体库'},
             {'id': '200', 'name': '电影'}, {'id': '300', 'name': '作品'}]
    cloud = SimpleNamespace(
        file=AsyncMock(return_value={'id': '123', 'parent_id': '300', 'name': name, 'size': 1234}),
        path_info=AsyncMock(return_value=trail))
    store = {'console_config': {'emby_strm_path': '/library/'}, 'emby_proxy_servers': [
        {'id': 'secondary', 'upstream': 'http://secondary.test', 'api_key': 'key', 'emby_strm_path': '/secondary'}]}
    engine = SimpleNamespace(root=tmp_path, store=store)
    engine.sync = Sync(engine)
    app.state.organizer, app.state.cloud = engine, cloud
    install_phase5(app, store, lambda: True, None)
    with TestClient(app) as client:
        response = client.post('/api/v1/sync/profiles/preview-path',
                               json={'source_cid': root, 'file_id': '123', 'emby_target': target})
    assert response.status_code == status
    if status == 200:
        value = response.json()
        assert value['cloud_path'] == f'/媒体库/电影/作品/{name}'
        if skipped:
            assert value['skip_reason'] and 'local_path' not in value
        else:
            assert value['local_path'] == str(tmp_path / '媒体库' / '电影' / '作品' / '测试片.strm')
            assert value['emby_path'] == (mount + '/媒体库/电影/作品/测试片.strm' if mount else None)
    cloud.file.assert_awaited_once_with('123')
    cloud.path_info.assert_awaited_once_with('300')
    assert not list(tmp_path.iterdir())
