"""Share episode naming must never treat the workflow or share code as a title."""
import asyncio
import json

import pytest

from strimkeep.media import NeedsReview
from strimkeep.share_service import share_recognition_hint
from strimkeep import share_service
from test_phase2_share_pipeline import share_setup


def row(name, path, code='swsu14c3wsy'):
    return {'name': name, 'strm_path': path, 'share_code': code}


def test_share_episode_uses_original_evidenced_work_directory():
    hint = share_recognition_hint(row('S01E01-1080P.mkv',
        '分享STRM待整理/swsu14c3wsy/璀璨之城 (2026)国粤双语 繁中字幕 1080P Hami源/S01E01-1080P.mkv.strm'))
    assert (hint['query'], hint['year'], hint['season'], hint['episode'], hint['media_type']) == ('璀璨之城', 2026, 1, 1, 'tv')
    assert hint['identity_source'] == 'ancestor_directory'


def test_share_bare_number_uses_work_and_season_directory():
    hint = share_recognition_hint(row('03.mkv', '分享STRM待整理/swsu14c3wsy/测试剧集 (2024)/Season 2/03.mkv.strm'))
    assert (hint['query'], hint['year'], hint['season'], hint['episode']) == ('测试剧集', 2024, 2, 3)


@pytest.mark.parametrize('path', [
    '分享STRM待整理/S01E01-1080P.mkv.strm',
    '分享STRM待整理/swsu14c3wsy/S01E01-1080P.mkv.strm',
    '分享STRM待整理/swsu14c3wsy/电视剧/S01E01-1080P.mkv.strm',
    '分享STRM待整理/swsu14c3wsy/测试剧集/S01E01-1080P.mkv.strm',
    '分享STRM待整理/another-share/测试剧集 (2024)/S01E01-1080P.mkv.strm',
    '分享STRM待整理/swsu14c3wsy/电视剧 (2024)/S01E01-1080P.mkv.strm',
])
def test_share_code_generic_or_unverified_folder_is_not_title(path):
    with pytest.raises(NeedsReview):
        share_recognition_hint(row('S01E01-1080P.mkv', path))


def test_share_episode_rejects_conflicting_season_and_combined_episodes():
    with pytest.raises(NeedsReview):
        share_recognition_hint(row('S01E01.mkv', '分享STRM待整理/swsu14c3wsy/测试剧集 (2024)/Season 2/S01E01.mkv.strm'))
    with pytest.raises(NeedsReview):
        share_recognition_hint(row('S01E01-E02.mkv', '分享STRM待整理/swsu14c3wsy/测试剧集 (2024)/S01E01-E02.mkv.strm'))


def test_share_pipeline_organizes_bare_episodes_using_parent(tmp_path):
    async def scenario():
        store, cloud, http, engine, requests = share_setup(tmp_path)
        share_service.generate(store, engine.root, {'share_code':'test-code','receive_code':''}, [
            {'id':'301','name':'S01E03-1080P.mkv','size':5000,'path':'测试剧集 (2024)/'},
        ])
        jid = share_service.enqueue_organize(engine, origin='web')
        await engine.run_job(jid)
        item = store.db.execute('SELECT state,plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        assert item['state'] == 'done'
        plan = json.loads(item['plan'])
        assert plan['hint']['query'] == '测试剧集'
        assert plan['hint']['episode'] == 3
        assert plan['relative_path'].startswith('分享影视库/剧集/')
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        await http.aclose()
        store.close()
    asyncio.run(scenario())
