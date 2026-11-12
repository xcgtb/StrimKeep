"""NFO 与海报：内容对不对、开关关掉时是否不写、写失败是否影响整理。"""
import asyncio

from strimkeep.metadata_files import build_nfo, build_tvshow_nfo, write_sidecars
from test_organize import CONFIG, MOVIE, TV, new_job, setup


def plan_for(details, media_type='movie', season=None, episode=None, episodes=()):
    return {'hint': {'media_type': media_type, 'season': season, 'episode': episode},
            'details': details, 'episodes': list(episodes)}


def test_movie_nfo_has_identity_and_title():
    nfo = build_nfo(plan_for(MOVIE))
    assert '<movie>' in nfo and '<title>测试电影</title>' in nfo
    assert '<year>2024</year>' in nfo
    assert '<uniqueid type="tmdb" default="true">20</uniqueid>' in nfo
    assert '<genre>剧情</genre>' in nfo


def test_episode_nfo_carries_season_and_show():
    episode = {'name': '第3集', 'overview': '单集剧情', 'air_date': '2024-01-03'}
    nfo = build_nfo(plan_for(TV, 'tv', season=1, episode=3, episodes=[episode]))
    assert '<episodedetails>' in nfo
    assert '<title>第3集</title>' in nfo and '<showtitle>测试剧集</showtitle>' in nfo
    assert '<season>1</season>' in nfo and '<episode>3</episode>' in nfo
    show = build_tvshow_nfo(plan_for(TV, 'tv', season=1, episode=3))
    assert '<tvshow>' in show
    assert '测试剧集' in show and '<uniqueid type="tmdb" default="true">10</uniqueid>' in show


def test_missing_details_produce_no_nfo():
    assert build_nfo({'hint': {'media_type': 'movie'}, 'details': {}}) == ''


def test_show_nfo_is_not_rewritten_when_unchanged(tmp_path):
    """tvshow.nfo 全集共用：内容没变就不重写，避免每集刷新 mtime / 多算一个 nfo。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        target = engine.root / '剧' / 'Season 1' / 'ep.strm'
        target.parent.mkdir(parents=True)
        target.write_text('x')
        plan = plan_for(TV, 'tv', season=1, episode=1)
        first = await write_sidecars(engine, target, plan, {'generate_nfo': True})
        show = engine.root / '剧' / 'tvshow.nfo'
        assert show.is_file() and str(show.relative_to(engine.root)) in first
        mtime = show.stat().st_mtime_ns
        second = await write_sidecars(engine, target, plan, {'generate_nfo': True})
        assert str(show.relative_to(engine.root)) not in second
        assert show.stat().st_mtime_ns == mtime
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_switches_off_write_nothing(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        target = tmp_path / 'strm' / 'movie' / '片子.strm'
        target.parent.mkdir(parents=True)
        target.write_text('x')
        assert await write_sidecars(engine, target, plan_for(MOVIE), {}) == []
        assert await write_sidecars(engine, target, plan_for(MOVIE), {'generate_nfo': False}) == []
        assert not list(target.parent.glob('*.nfo'))
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_organize_writes_episode_nfo_tvshow_nfo_and_poster(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1000', '测试剧集 (2024) S01E03.WEB-DL.mkv')
        jid = new_job(store, {**CONFIG, 'strm_options': {
            'generate_nfo': True, 'generate_images': True,
            'inherit_video_extensions': False, 'video_extensions': ['.mkv', '.mp4'],
            'metadata_extensions': ['.srt'], 'min_video_size_mb': 0, 'cleanup_empty': True}})

        await engine.run_job(jid)

        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        root = tmp_path / 'strm'
        nfos = sorted(root.rglob('*.nfo'))
        assert len(nfos) == 2, [p.name for p in nfos]        # 单集 + tvshow
        assert any(p.name == 'tvshow.nfo' for p in nfos)
        posters = list(root.rglob('poster.jpg'))
        assert posters and posters[0].stat().st_size > 0, '海报应当写到作品目录'
        episode_nfo = next(p for p in nfos if p.name != 'tvshow.nfo')
        text = episode_nfo.read_text(encoding='utf-8')
        assert '<episodedetails>' in text and '<episode>3</episode>' in text
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_bad_image_path_is_skipped_without_breaking(tmp_path):
    """海报路径可疑时只跳过图片，NFO 照写，整理不受影响。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        target = tmp_path / 'strm' / 'movie' / '片子.strm'
        target.parent.mkdir(parents=True)
        target.write_text('x')
        plan = plan_for({**MOVIE, 'poster_path': '../evil.jpg', 'backdrop_path': None})

        written = await write_sidecars(engine, target, plan,
                                       {'generate_nfo': True, 'generate_images': True})

        assert any(path.endswith('.nfo') for path in written)
        assert not list(target.parent.glob('*.jpg'))
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_existing_sync_backfills_missing_sidecars_without_overwriting(tmp_path):
    from test_phase5 import sync_config
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        source = cloud.add_video(name='测试电影 (2024).mkv'); source['sha1'] = 'A' * 40
        identify = engine.identify
        async def identify_with_art(hint):
            return {**await identify(hint), 'poster_path':'/poster.jpg', 'backdrop_path':'/backdrop.jpg'}
        engine.identify = identify_with_art
        config = {**sync_config(), 'notify_emby':False}
        first = new_job(store, config); await engine.run_job(first)
        relative = engine.outputs.get('1','1000')['relative_path']
        target = engine.root / relative
        stream = target.read_bytes()
        user_nfo = target.with_suffix('.nfo'); user_nfo.write_text('用户的 NFO')
        second = new_job(store, {**config,'generate_nfo':True,'generate_images':True})
        await engine.run_job(second)
        assert engine.job(second)['state'] == 'completed', dict(engine.job(second))
        assert target.read_bytes() == stream
        assert user_nfo.read_text() == '用户的 NFO'
        assert (target.parent/'poster.jpg').is_file()
        user_nfo.unlink()
        third = new_job(store, {**config,'generate_nfo':True})
        await engine.run_job(third)
        assert '<movie>' in user_nfo.read_text(encoding='utf-8')
        assert not cloud.writes
        await http.aclose();store.close()
    asyncio.run(run())


def test_unchanged_sync_schedules_missing_media_info(tmp_path):
    import json
    import httpx
    from strimkeep.store import digest
    from test_phase5 import sync_config
    async def run(monkeypatch=None):
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'sync_config':{'emby_media_info':True}})
        source = cloud.add_video(name='测试电影 (2024).mkv'); source['sha1'] = 'A' * 40
        first = new_job(store, {**sync_config(),'notify_emby':False}); await engine.run_job(first)
        second = new_job(store, {**sync_config(),'emby_media_info':True}); await engine.run_job(second)
        row = store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(second,)).fetchone()
        assert json.loads(row['plan'])['output_changed'] is True
        targets = engine.emby.targets(second)
        assert targets
        engine.emby.connection=lambda: ('http://emby','test-key')
        conn=digest('http://emby|test-key')
        store.db.execute('INSERT INTO emby_deliveries(job_id,state,payload,next_due,deadline) VALUES(?,?,?,?,?)',
            (second,'checking',json.dumps({'connection':conn,'targets':targets}),0,100))
        async def items(_params,_headers,_url):
            return [{'Id':'item1','Path':'/strimkeep/'+targets[0]['relative']}]
        engine.emby.items=items
        result=await engine.emby.verify(second)
        assert result['confirmed']==1
        assert store.db.execute('SELECT state FROM native_media_info_queue WHERE job_id=?',(second,)).fetchone()['state']=='queued'
        assert not cloud.writes
        await http.aclose();store.close()
    asyncio.run(run())
