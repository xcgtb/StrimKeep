import asyncio

import pytest

from strimkeep.media import NeedsReview, classify, work_parts
from test_organize import CONFIG, new_job, setup


KIDNAP = {
    'id': 320513, 'name': '绑架游戏', 'first_air_date': '2026-10-06',
    'origin_country': ['HK', 'JP', 'KR'],
    'production_countries': [{'iso_3166_1': 'JP'}, {'iso_3166_1': 'HK'}, {'iso_3166_1': 'KR'}],
    'genres': [{'id': 18}],
}


def test_multiregion_and_missing_region_keep_known_work():
    assert classify(KIDNAP, 'tv') == ('剧集', '其他')
    assert classify({**KIDNAP, 'origin_country': [], 'production_countries': []}, 'tv') == ('剧集', '其他')
    movie = {'title': '测试电影', 'origin_country': ['CN', 'US'], 'genres': []}
    assert classify(movie, 'movie') == ('电影', '其他')


def test_single_region_and_multiple_countries_in_same_region_stay_unchanged():
    assert classify({**KIDNAP, 'origin_country': ['JP']}, 'tv') == ('剧集', '日韩')
    assert classify({**KIDNAP, 'origin_country': ['JP', 'KR']}, 'tv') == ('剧集', '日韩')
    assert classify({**KIDNAP, 'origin_country': [], 'production_countries': [{'iso_3166_1': 'US'}]}, 'tv') == ('剧集', '欧美')


def test_region_disabled_still_keeps_genre_and_music_safety():
    hint = {'media_type': 'tv', 'season': 1}
    parts, category, _ = work_parts(hint, KIDNAP, {'classify_enabled': True, 'region_enabled': False})
    assert category == '剧集' and parts == ['剧集', '绑架游戏 (2026) {tmdb-320513}', 'Season 1']
    with pytest.raises(NeedsReview, match='音乐类电影'):
        classify({'title': '音乐电影', 'origin_country': [], 'production_countries': [], 'genres': [{'id': 10402}]}, 'movie')


def test_kidnap_co_production_organizes_in_other_tv(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video(name='kiDnap GAME.2026.S01E01.1080p.mkv')
        async def identify(hint): return dict(KIDNAP)
        async def tmdb(path, **kwargs): return {'season_number': 1, 'episode_number': 1, 'name': '第1集'}
        engine.identify = identify
        engine.tmdb = tmdb
        jid = new_job(store, {**CONFIG, 'notify_emby': False})
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        files = list((tmp_path / 'strm').rglob('*.strm'))
        assert len(files) == 1 and files[0].parts[-5:-1] == ('剧集', '其他剧集', '绑架游戏 (2026) {tmdb-320513}', 'Season 1')
        await http.aclose()
        store.close()
    asyncio.run(scenario())
