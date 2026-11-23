from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from strimkeep.main import create_app
from strimkeep.media import classification, work_parts


def test_classification_reasons_explain_region_fallbacks_and_animation_layout():
    details = {'id': 42, 'title': '示例动画', 'release_date': '2024-01-01',
               'genres': [{'id': 16}, {'id': 10751}], 'origin_country': ['HK', 'JP']}
    decision = classification(details, 'movie')
    assert decision.category == '动画电影'
    assert '不设地区子目录' in decision.region_reason
    parts, category, region = work_parts({'media_type': 'movie'}, details)
    assert parts[:2] == ['电影', '动画电影'] and (category, region) == (decision.category, decision.region)

    decision = classification(details, 'tv')
    assert decision.category == '儿童节目' and decision.region == '其他'
    assert '动画（16）与家庭（10751）' in decision.category_reason
    assert 'HK、JP' in decision.region_reason and '跨多个分类地区' in decision.region_reason

    fallback = {**details, 'origin_country': [], 'production_countries': [{'iso_3166_1': 'MO'}]}
    decision = classification(fallback, 'tv', {'children_enabled': False})
    assert decision.category == '动漫' and decision.region == '国产'
    assert '已关闭' in decision.category_reason and '制片国家为 MO' in decision.region_reason


def test_preview_uses_each_library_saved_rules_and_never_enqueues(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm')
    store = app.state.store
    details = {'id': 42, 'name': '示例节目', 'first_air_date': '2024-01-01',
               'genres': [{'id': 16, 'name': '动画'}, {'id': 10751, 'name': '家庭'}],
               'origin_country': ['US']}
    episode = {'season_number': 1, 'episode_number': 1, 'name': '示例单集'}
    app.state.organizer = SimpleNamespace(
        identify=AsyncMock(return_value=details),
        tmdb_episode=AsyncMock(return_value=episode),
        tmdb=AsyncMock(return_value=episode))
    store.set_many({'share_profile': {'organize': {'children_enabled': False}, 'upgrade_policy': {}}})
    token, csrf = store.new_session()
    body = {'name': '示例节目.2024.S01E01.1080p.mkv'}
    try:
        client = TestClient(app, headers={'X-CSRF-Token': csrf}, cookies={'strimkeep_session': token})
        try:
            cloud = client.post('/api/v1/organize/preview', json=body)
            share = client.post('/api/v1/organize/preview', json={**body, 'library': 'share'})
            parsed = client.post('/api/v1/organize/preview', json={**body, 'use_tmdb': False})
        finally:
            client.close()
        assert cloud.status_code == share.status_code == parsed.status_code == 200
        assert cloud.json()['parts'][:2] == ['儿童节目', '欧美儿童节目']
        assert share.json()['parts'][:2] == ['动漫', '欧美动漫']
        assert '儿童节目独立分类已关闭' in share.json()['classification']['category_reason']
        assert share.json()['classification']['genres'] == details['genres']
        assert parsed.json()['stage'] == 'parsed' and 'classification' not in parsed.json()
        app.state.organizer.tmdb_episode.assert_awaited_once_with(42, 1, 1)
        app.state.organizer.tmdb.assert_awaited_once_with('tv/42/season/1/episode/1')
        assert store.db.execute('SELECT COUNT(*) FROM organize_jobs').fetchone()[0] == 0
        assert not list((tmp_path / 'strm').iterdir())
    finally:
        store.close()
