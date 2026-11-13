from strimkeep.media import bind_subtitles


def entry(fid, name):
    return {'id': fid, 'name': name, 'parent_id': '1'}


def test_subtitle_fallback_matches_complete_episode_range():
    videos = bind_subtitles([
        entry('v1', '剧名.S01E01.1080p-GROUP.mkv'),
        entry('v2', '剧名.S01E01-E02.2160p-GROUP.mkv'),
        entry('s1', '剧名.S01E01.另一发布组.chs.srt'),
        entry('s2', '剧名.S01E01-E02.另一发布组.chs.srt'),
    ])
    by_id = {v['id']: v for v in videos}
    assert [s['id'] for s in by_id['v1']['subtitles']] == ['s1']
    assert [s['id'] for s in by_id['v2']['subtitles']] == ['s2']


def test_single_episode_subtitle_does_not_attach_to_combined_episode():
    videos = bind_subtitles([
        entry('v', '剧名.S01E01-E02.2160p-GROUP.mkv'),
        entry('s', '剧名.S01E01.其他发布组.chs.srt'),
    ])
    assert videos[0]['subtitles'] == []


def test_multiple_same_range_versions_remain_ambiguous():
    videos = bind_subtitles([
        entry('v1', '剧名.S01E01-E02.1080p-A.mkv'),
        entry('v2', '剧名.S01E01-E02.2160p-B.mkv'),
        entry('s', '剧名.S01E01-E02.C.chs.srt'),
    ])
    assert all(not v['subtitles'] and v['subtitle_warning'] for v in videos)


def test_episode_fallback_does_not_match_another_show_in_same_folder():
    videos = bind_subtitles([
        entry('v', '甲剧.S01E01.1080p-GROUP.mkv'),
        entry('s', '乙剧.S01E01.其他发布组.chs.srt'),
    ])
    assert videos[0]['subtitles'] == []


def test_combined_video_and_subtitle_enter_unrecognized_directory(tmp_path):
    import asyncio
    from test_organize import setup, new_job, CONFIG

    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        name = '测试剧集 (2024) S01E03-E04.mkv'
        subtitle = '测试剧集 (2024) S01E03-E04.chs.srt'
        cloud.add_video('1000', name)
        cloud.nodes['1100'] = {'id': '1100', 'name': subtitle, 'parent_id': '100',
                               'is_dir': False, 'size': 10, 'pickcode': 'p1100'}
        jid = new_job(store, {**CONFIG, 'retries': 0})
        await engine.run_job(jid)
        row = store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?', (jid,)).fetchone()
        assert row['state'] == 'pending' and '包含多集' in row['message']
        assert cloud.nodes['1000']['name'] == name and cloud.nodes['1000']['parent_id'] == '300'
        assert cloud.nodes['1100']['name'] == subtitle and cloud.nodes['1100']['parent_id'] == '300'
        assert not list(engine.root.rglob('*.strm'))
        await http.aclose()
        store.close()
    asyncio.run(scenario())


def test_combined_video_without_unrecognized_directory_stays_in_place(tmp_path):
    import asyncio
    from test_organize import setup, new_job, CONFIG

    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        name = '测试剧集 (2024) S01E03-E04.mkv'
        cloud.add_video('1000', name)
        jid = new_job(store, {**CONFIG, 'pending_cid': '', 'retries': 0})
        await engine.run_job(jid)
        row = store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?', (jid,)).fetchone()
        assert row['state'] == 'skipped' and '包含多集' in row['message']
        assert cloud.nodes['1000']['name'] == name and cloud.nodes['1000']['parent_id'] == '100'
        assert not list(engine.root.rglob('*.strm'))
        await http.aclose()
        store.close()
    asyncio.run(scenario())

def test_chinese_and_ep_only_subtitles_match_scene_video():
    """现场：视频写 SxxExx、字幕写「第05集」/「EP05」，episode_key 只认 SxxExx 时配不上。"""
    videos = bind_subtitles([
        entry('v', '剧名.S01E05.1080p-GROUP.mkv'),
        entry('s1', '剧名.第05集.chs.srt'),
        entry('s2', '剧名.EP05.简中.ass'),
    ])
    by_id = {v['id']: v for v in videos}
    assert sorted(sub['id'] for sub in by_id['v']['subtitles']) == ['s1', 's2']


def test_episode_key_agrees_between_naming_styles():
    """parse_name 与 episode_key 必须给同一份季集范围，否则字幕/同目录判断会分叉。"""
    from strimkeep.media import episode_key

    scene = episode_key('剧名.S01E05.1080p-GROUP.mkv')
    chinese = episode_key('剧名.第05集.chs.srt')
    ep_only = episode_key('剧名.EP05.简中.ass')
    assert scene is not None and chinese is not None and ep_only is not None
    assert scene == chinese == ep_only
    assert scene[1:] == (1, 5, 5)
    assert episode_key('剧名.S01E05-E06.2160p.mkv')[1:] == (1, 5, 6)
