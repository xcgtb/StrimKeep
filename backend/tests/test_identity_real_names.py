"""真实命名回归：年份在季集后、父目录 ID、Season 子目录、冲突与纯数字。"""
import asyncio

import httpx
import pytest

from strimkeep import media
from strimkeep.media import NeedsReview, folder_context, parse_name, require_work_identity
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import CONFIG, MemoryCloud, good_probe, new_job

NAME = '小城良方.S01E01.2026.2160p.WEB-DL.H265.DV.25fps.DDP5.1.mkv'
FOLDER = '小城良方 (2026)[tmdbid=285625]杜比视界'
SEASON_DIR = FOLDER + '/Season 1'


def test_year_after_season_episode_is_kept():
    hint = parse_name(NAME)
    assert hint['year'] == 2026 and hint['query'] == '小城良方'
    assert hint['media_type'] == 'tv' and (hint['season'], hint['episode']) == (1, 1)


def test_year_before_season_episode_still_works():
    hint = parse_name('小城良方.2026.S01E01.2160p.WEB-DL.H265.DV.25fps.DDP5.1.mkv')
    assert hint['year'] == 2026 and hint['query'] == '小城良方'


def test_parent_folder_supplies_identity_and_year():
    hint = parse_name(NAME, FOLDER)
    assert hint['tmdb_id'] == 285625 and hint['year'] == 2026
    assert hint['query'] == '小城良方' and hint['season'] == 1


def test_season_subdirectory_keeps_series_identity():
    assert folder_context(SEASON_DIR) == {'title': '小城良方', 'year': 2026, 'season': 1, 'tmdb_id': 285625}
    hint = parse_name(NAME, SEASON_DIR)
    assert hint['tmdb_id'] == 285625 and hint['season'] == 1


def test_braced_tmdb_marker_and_nested_season():
    hint = parse_name('Some.Show.S02E03.1080p.WEB-DL.mkv', 'Some Show (2021) {tmdb-4242}/Season 2')
    assert hint['tmdb_id'] == 4242 and hint['year'] == 2021 and hint['query'] == 'Some Show'
    assert (hint['season'], hint['episode']) == (2, 3)


def test_file_and_folder_tmdb_conflict_is_refused():
    with pytest.raises(NeedsReview) as err:
        parse_name('小城良方.S01E01.2026.2160p.{tmdb-999}.mkv', FOLDER)
    assert 'TMDB 标识不一致' in str(err.value)


def test_file_and_folder_season_conflict_is_refused():
    with pytest.raises(NeedsReview) as err:
        parse_name('小城良方.S02E01.2026.2160p.mkv', SEASON_DIR)
    assert '季号不一致' in str(err.value)


def test_specials_season_zero_is_not_treated_as_conflict():
    hint = parse_name('小城良方.S00E01.2026.2160p.mkv', SEASON_DIR)
    assert hint['season'] == 0 and hint['tmdb_id'] == 285625


def test_bare_number_without_context_is_never_searched():
    for name in ('31.mkv', '11.mkv'):
        hint = parse_name(name)
        with pytest.raises(NeedsReview) as err:
            require_work_identity(hint)
        assert '只有集号' in str(err.value)


def test_bare_number_uses_a_reliable_parent_folder():
    hint = parse_name('11.mkv', FOLDER)
    assert hint['media_type'] == 'tv' and hint['episode'] == 11
    assert hint['tmdb_id'] == 285625 and hint['query'] == '小城良方'
    require_work_identity(hint)


def test_bare_number_in_a_generic_directory_is_not_matched():
    # 「未识别」这种目录不是作品目录：不能被当成剧名，也不能猜集数。
    hint = parse_name('11.mkv', '未识别')
    with pytest.raises(NeedsReview):
        require_work_identity(hint)


def test_year_in_folder_is_not_copied_to_movies():
    hint = parse_name('某片.2008.1080p.mkv', '某剧 (2026)[tmdbid=1]')
    assert hint['media_type'] == 'movie' and hint['year'] == 2008 and hint['tmdb_id'] is None


def test_chinese_episode_and_merged_range_are_unchanged():
    hint = parse_name('小城良方.S01E05.第05集.2160p.mkv', FOLDER)
    assert hint['episode'] == 5 and hint['end_episode'] == 5
    merged = parse_name('小城良方.S01E01-E02.2160p.mkv', FOLDER)
    assert (merged['episode'], merged['end_episode']) == (1, 2)


def test_dv_and_hdr_releases_keep_the_same_episode_identity():
    dv = parse_name('小城良方.S01E13.2026.2160p.WEB-DL.H265.DV.25fps.DDP5.1.mkv', FOLDER)
    hdr = parse_name('小城良方.S01E13.2026.2160p.WEB-DL.H265.HDR.25fps.DDP5.1.mkv', FOLDER)
    assert (dv['season'], dv['episode']) == (hdr['season'], hdr['episode']) == (1, 13)
    assert dv['tmdb_id'] == hdr['tmdb_id'] == 285625


def _engine(tmp_path, handler):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test', 'tmdb_key': 'secret-key',
                    'playback_base': 'https://play.example', 'emby_url': 'http://emby:8096',
                    'emby_key': 'emby-secret'})
    cloud = MemoryCloud(store)
    calls = []

    def traced(request):
        calls.append(request.url.path)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(traced))
    engine = Organizer(store, cloud, http, tmp_path / 'strm', good_probe)
    return store, cloud, http, engine, calls


def test_bare_number_job_stops_before_any_tmdb_request(tmp_path):
    async def run():
        def handler(request):
            raise AssertionError('纯数字文件不应发起 TMDB 请求：' + request.url.path)

        store, cloud, http, engine, calls = _engine(tmp_path, handler)
        cloud.add_video(name='31.mkv')
        job = new_job(store, CONFIG)
        await engine.run_job(job)
        row = store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?', (job,)).fetchone()
        assert row['state'] == 'pending' and '只有集号' in row['message']
        assert calls == []
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_tmdb_without_unique_match_keeps_the_file(tmp_path):
    async def run():
        def handler(request):
            if request.url.path.startswith('/3/search/'):
                return httpx.Response(200, json={'results': []})
            raise AssertionError('unexpected ' + request.url.path)

        store, cloud, http, engine, calls = _engine(tmp_path, handler)
        cloud.add_video(name='某剧.S01E01.2026.1080p.WEB-DL.mkv')
        job = new_job(store, CONFIG)
        await engine.run_job(job)
        row = store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?', (job,)).fetchone()
        assert row['state'] == 'pending' and 'TMDB 未找到' in row['message']
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_tmdb_failure_is_reported_not_guessed(tmp_path):
    async def run():
        def handler(request):
            return httpx.Response(500, json={})

        store, cloud, http, engine, calls = _engine(tmp_path, handler)
        cloud.add_video(name='某剧.S01E01.2026.1080p.WEB-DL.mkv')
        job = new_job(store, CONFIG)
        await engine.run_job(job)
        row = store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?', (job,)).fetchone()
        assert row['state'] in ('pending', 'review')
        assert '请求失败' in row['message'] or '未完成' in row['message']
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_folder_identity_skips_the_search_request(tmp_path):
    async def run():
        def handler(request):
            if request.url.path == '/3/tv/285625':
                return httpx.Response(200, json={'id': 285625, 'name': '小城良方',
                                                 'original_name': '小城良方'})
            raise AssertionError('unexpected ' + request.url.path)

        store, cloud, http, engine, calls = _engine(tmp_path, handler)
        details = await engine.identify(parse_name(NAME, FOLDER))
        assert details['id'] == 285625
        assert calls == ['/3/tv/285625']
        await http.aclose()
        store.close()
    asyncio.run(run())
