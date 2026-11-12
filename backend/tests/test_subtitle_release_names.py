import asyncio
from pathlib import Path

import pytest

from strimkeep.media import subtitle_suffix
from test_organize import setup, new_job, CONFIG


@pytest.mark.parametrize(('video', 'subtitle', 'expected'), [
    ('剧名.S01E01.2160p-GROUP.mkv', '剧名.S01E01.1080p-OTHER.chs.forced.srt', '.chs.forced.srt'),
    ('剧名.S01E01.2160p-GROUP.mkv', '剧名.S01E01.OTHER.zh-Hant.ass', '.zh-Hant.ass'),
    ('剧名.S01E01.mkv', '剧名.S01E01.cht.forced.srt', '.cht.forced.srt'),
    ('剧名.S01E01.2160p-GROUP.mkv', '剧名.S01E01.OTHER.srt', '.srt'),
])
def test_subtitle_suffix_keeps_extension_and_explicit_tags(video, subtitle, expected):
    assert subtitle_suffix(video, subtitle) == expected


def test_other_release_subtitle_is_renamed_and_exported_with_video(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video('1000', '测试剧集 (2024) S01E03.2160p.GROUP.mkv')
        sub = cloud.add_video('1001', '测试剧集 (2024) S01E03.1080p.OTHER.chs.forced.srt')
        sub['size'] = 50
        jid = new_job(store, {**CONFIG, 'notify_emby': False})
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        assert cloud.nodes['1001']['name'] == Path(cloud.nodes['1000']['name']).stem + '.chs.forced.srt'
        assert cloud.nodes['1001']['parent_id'] == cloud.nodes['1000']['parent_id']
        strm = next(engine.root.rglob('*.strm'))
        assert strm.with_suffix('.chs.forced.srt').is_file()
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_same_target_subtitles_never_overwrite_each_other(tmp_path):
    async def scenario():
        from test_upgrade import upgraded_setup
        store, cloud, http, engine, _, _ = upgraded_setup(tmp_path)
        name = '测试剧集 (2024) S01E03.2160p.GROUP.mkv'
        video = cloud.add_video('1000', name)
        a = cloud.add_video('1001', '测试剧集 (2024) S01E03.A.chs.srt')
        b = cloud.add_video('1002', '测试剧集 (2024) S01E03.B.chs.srt')
        jid = new_job(store, {**CONFIG, 'pending_cid': '', 'notify_emby': False})
        await engine.run_job(jid)
        row = store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?', (jid,)).fetchone()
        assert row['state'] == 'done' and '字幕' in row['message']
        assert cloud.nodes['1001']['parent_id'] == cloud.nodes['1000']['parent_id']
        assert '1002' in cloud.bin and '1002' not in cloud.nodes
        assert list(engine.root.rglob('*.strm'))
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_sync_uses_release_independent_subtitle_name(tmp_path):
    from test_phase5 import sync_config
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1000', '测试剧集 (2024) S01E03.2160p.GROUP.mkv')
        cloud.add_video('1001', '测试剧集 (2024) S01E03.OTHER.chs.srt')['size'] = 50
        jid = new_job(store, {**sync_config(), 'notify_emby': False})
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        strm = next(engine.root.rglob('*.strm'))
        assert strm.with_suffix('.chs.srt').is_file()
        assert cloud.nodes['1001']['parent_id'] == '100'
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_sync_does_not_overwrite_colliding_subtitles(tmp_path):
    from test_phase5 import sync_config
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1000', '测试剧集 (2024) S01E03.2160p.GROUP.mkv')
        cloud.add_video('1001', '测试剧集 (2024) S01E03.A.chs.srt')['size'] = 50
        cloud.add_video('1002', '测试剧集 (2024) S01E03.B.chs.srt')['size'] = 50
        jid = new_job(store, {**sync_config(), 'notify_emby': False})
        await engine.run_job(jid)
        strm = next(engine.root.rglob('*.strm'))
        assert not strm.with_suffix('.chs.srt').exists()
        assert engine.job(jid)['state'] == 'partial'
        assert all(cloud.nodes[key]['parent_id'] == '100' for key in ('1001', '1002'))
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_sync_preserves_existing_subtitle_when_new_collision_appears(tmp_path):
    from test_phase5 import sync_config
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1000', '测试剧集 (2024) S01E03.2160p.GROUP.mkv')
        cloud.add_video('1001', '测试剧集 (2024) S01E03.A.chs.srt')['size'] = 50
        config = {**sync_config(), 'notify_emby': False}
        await engine.run_job(new_job(store, config))
        strm = next(engine.root.rglob('*.strm'))
        subtitle = strm.with_suffix('.chs.srt')
        prior = subtitle.read_bytes()
        cloud.add_video('1002', '测试剧集 (2024) S01E03.B.chs.srt')['size'] = 50
        jid = new_job(store, config)
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial'
        assert subtitle.read_bytes() == prior and strm.exists()
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_movie_subtitles_match_different_release_only_when_unique():
    from strimkeep.media import bind_subtitles
    files=[{'id':'1','parent_id':'100','name':'The.Matrix.1999.2160p.WEB-DL.mkv'},
           {'id':'2','parent_id':'100','name':'The.Matrix.1999.1080p.BluRay.chs.srt'}]
    result=bind_subtitles(files)
    assert [s['id'] for s in result[0]['subtitles']]==['2']
    files.append({'id':'3','parent_id':'100','name':'The.Matrix.1999.720p.REMUX.mkv'})
    assert all(not v['subtitles'] for v in bind_subtitles(files))


def test_movie_subtitles_with_conflicting_year_do_not_match():
    from strimkeep.media import bind_subtitles
    files=[{'id':'1','parent_id':'100','name':'The.Matrix.1999.2160p.WEB-DL.mkv'},
           {'id':'2','parent_id':'100','name':'The.Matrix.2003.1080p.BluRay.chs.srt'}]
    assert not bind_subtitles(files)[0]['subtitles']


def test_release_fallback_only_uses_videos_in_the_same_directory():
    from strimkeep.media import bind_subtitles
    files = [
        {'id': '1', 'parent_id': '100', 'name': 'The.Matrix.1999.2160p.WEB-DL.mkv'},
        {'id': '2', 'parent_id': '200', 'name': 'The.Matrix.1999.720p.REMUX.mkv'},
        {'id': '3', 'parent_id': '100', 'name': 'The.Matrix.1999.1080p.BluRay.chs.srt'},
        {'id': '4', 'parent_id': '300', 'name': 'Show.S01E03.2160p.GROUP.mkv'},
        {'id': '5', 'parent_id': '400', 'name': 'Show.S01E03.720p.OTHER.mkv'},
        {'id': '6', 'parent_id': '300', 'name': 'Show.S01E03.1080p.OTHER.chs.srt'},
    ]
    videos = {v['id']: v for v in bind_subtitles(files)}
    assert [s['id'] for s in videos['1']['subtitles']] == ['3']
    assert not videos['2']['subtitles']
    assert [s['id'] for s in videos['4']['subtitles']] == ['6']
    assert not videos['5']['subtitles']


def test_episode_fallback_retains_ambiguity_within_directory():
    from strimkeep.media import bind_subtitles
    files = [
        {'id': '1', 'parent_id': '100', 'name': 'Show.S01E03.2160p.A.mkv'},
        {'id': '2', 'parent_id': '100', 'name': 'Show.S01E03.720p.B.mkv'},
        {'id': '3', 'parent_id': '100', 'name': 'Show.S01E03.1080p.C.chs.srt'},
    ]
    videos = bind_subtitles(files)
    assert all(not v['subtitles'] and v.get('subtitle_warning') for v in videos)
