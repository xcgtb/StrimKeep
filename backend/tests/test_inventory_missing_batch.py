"""批量剧集缺集对照接口测试（消除翻页 N+1 请求）。"""
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from strimkeep.inventory_api import install_inventory
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import MemoryCloud


def _make_app(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'playback_base': 'http://nas:9000',
                    'organize_config': {'library_dir_name': '影视媒体库', 'archive_cid': '100'}})
    strm_root = tmp_path / 'strm'
    show1 = strm_root / '影视媒体库/剧集/剧集A (2025) {tmdb-1001}/Season 1'
    show1.mkdir(parents=True, exist_ok=True)
    (show1 / '剧集A - S01E01.strm').write_text('http://nas:9000/play115/pc1/1/aa/100/a.strm\n', encoding='utf-8')

    show2 = strm_root / '影视媒体库/剧集/剧集B (2025) {tmdb-1002}/Season 1'
    show2.mkdir(parents=True, exist_ok=True)
    (show2 / '剧集B - S01E01.strm').write_text('http://nas:9000/play115/pc2/2/bb/100/b.strm\n', encoding='utf-8')

    def handler(request):
        path = request.url.path
        if path == '/3/tv/1001':
            return httpx.Response(200, json={'id': 1001, 'status': 'Ended', 'seasons': [{'season_number': 1}]})
        if path == '/3/tv/1001/season/1':
            return httpx.Response(200, json={'episodes': [
                {'episode_number': 1, 'air_date': '2025-01-01'},
                {'episode_number': 2, 'air_date': '2025-01-08'},
            ]})
        if path == '/3/tv/1002':
            return httpx.Response(200, json={'id': 1002, 'status': 'Ended', 'seasons': [{'season_number': 1}]})
        if path == '/3/tv/1002/season/1':
            return httpx.Response(200, json={'episodes': [
                {'episode_number': 1, 'air_date': '2025-01-01'},
            ]})
        return httpx.Response(404, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cloud = MemoryCloud(store)
    organizer = Organizer(store, cloud, http, strm_root)
    store.set_many({'tmdb_key': 'k'})

    app = FastAPI()
    app.state.store = store
    app.state.strms = strm_root.resolve()
    app.state.organizer = organizer
    app.state.http = http

    async def admin():
        return True

    install_inventory(app, store, admin)
    return app


def test_inventory_missing_batch_returns_items_and_errors(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        resp = client.post('/api/v1/inventory/tv/missing/batch', json={'tmdb_ids': [1001, 1002, 1001, 9999]})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body['items'].keys()) == {'1001', '1002'}
        assert body['items']['1001']['combined_missing'] == 1
        assert body['items']['1002']['combined_missing'] == 0
        assert body['errors'] == {'9999': '未找到该剧集。'}

        bad = client.post('/api/v1/inventory/tv/missing/batch', json={'tmdb_ids': [0]})
        assert bad.status_code == 422
