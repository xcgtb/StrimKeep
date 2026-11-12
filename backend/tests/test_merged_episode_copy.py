"""合并集提示必须与实际去向一致：没配置未识别目录时不能说已转走。"""
import asyncio

from strimkeep.media import NeedsReview, parse_name, require_single_episode
from test_organize import CONFIG, new_job, setup

MERGED = '测试剧集 (2024) S01E01-E02.WEB-DL.mkv'


def test_reason_does_not_claim_a_move():
    try:
        require_single_episode(parse_name(MERGED, ''), MERGED)
    except NeedsReview as exc:
        message = str(exc)
    else:
        raise AssertionError('合并集应被拒收')
    assert '合并集' in message and '已转未识别' not in message and '未识别' not in message


def test_without_pending_directory_keeps_the_file_and_says_so(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name=MERGED)
        config = {**CONFIG, 'pending_cid': ''}
        jid = new_job(store, config)
        await engine.run_job(jid)
        row = store.db.execute("SELECT state,message,step FROM organize_items WHERE job_id=?", (jid,)).fetchone()
        assert row['state'] == 'skipped'
        assert '已转未识别' not in row['message']
        assert '未配置未识别目录' in row['message'] and '原位保留' in row['message']
        assert cloud.nodes[video['id']]['parent_id'] == '100'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_with_pending_directory_reports_the_actual_transfer(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name=MERGED)
        jid = new_job(store, CONFIG)
        await engine.run_job(jid)
        row = store.db.execute("SELECT state,message,step FROM organize_items WHERE job_id=?", (jid,)).fetchone()
        assert row['state'] == 'pending'
        assert cloud.nodes[video['id']]['parent_id'] == '300'
        # 文件直接躺在来源根目录里：0.8.28 起逐个转移文件（绝不搬走来源目录本身）。
        assert '未识别文件已逐个转移' in row['step']
        assert '已转未识别' not in row['message']
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_split_episode_keeps_files_or_moves_to_pending(tmp_path):
    async def run(pending):
        store,cloud,http,engine,_=setup(tmp_path / ('pending' if pending else 'original'))
        files=[cloud.add_video(str(1100+i),f'测试剧集 (2024) S01E01.Part{i}.WEB-DL.mkv') for i in (1,2)]
        jid=new_job(store,{**CONFIG,'pending_cid':'300' if pending else ''})
        await engine.run_job(jid)
        rows=store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?',(jid,)).fetchall()
        assert len(rows)==2
        assert all(r['state']==('pending' if pending else 'skipped') for r in rows)
        assert all('分段视频' in r['message'] for r in rows)
        assert all(cloud.nodes[f['id']]['parent_id']==('300' if pending else '100') for f in files)
        assert not list(engine.root.rglob('*.strm'))
        await http.aclose();store.close()
    asyncio.run(run(False));asyncio.run(run(True))


def test_explicit_split_markers_and_title_are_distinct():
    for marker in ('Part1','Part2','CD1','CD2','Disc 1','上集','下集','上半集','下半集'):
        name=f'测试剧集 (2024) S01E01.{marker}.WEB-DL.mkv'
        try:
            require_single_episode(parse_name(name),name)
        except NeedsReview as exc:
            assert '分段视频' in str(exc)
        else:
            raise AssertionError(name)
    name='Part 1 Show (2024) S01E01.1080p.WEB-DL.mkv'
    assert require_single_episode(parse_name(name),name)['episode']==1


def test_split_episodes_do_not_block_complete_sibling(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        folder=(await cloud.mkdir('100','测试剧集 (2024)'))['cid']
        normal=cloud.add_video('1000','测试剧集 (2024) S01E02.WEB-DL.mkv',folder)
        parts=[cloud.add_video(str(1100+i),f'测试剧集 (2024) S01E01.Part{i}.WEB-DL.mkv',folder) for i in (1,2)]
        jid=new_job(store,CONFIG)
        await engine.run_job(jid)
        rows=store.db.execute('SELECT file_id,state FROM organize_items WHERE job_id=?',(jid,)).fetchall()
        states={r['file_id']:r['state'] for r in rows}
        assert states['1000']=='done',states
        assert all(states[f['id']]=='pending' for f in parts),states
        # 同一目录里已有成功整理的兄弟 → 逐个转移分段文件，作品目录留在原处。
        assert cloud.nodes[folder]['parent_id']=='100'
        assert cloud.nodes[normal['id']]['parent_id'] not in (folder,'300')
        assert len(list(engine.root.rglob('*.strm')))==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_movie_p1_p2_are_not_archived_as_complete(tmp_path):
    async def run(pending):
        store,cloud,http,engine,_=setup(tmp_path / ('pending' if pending else 'original'))
        files=[cloud.add_video(str(1200+i),f'测试电影 (2024).1080p.P{i}.mkv') for i in (1,2)]
        jid=new_job(store,{**CONFIG,'pending_cid':'300' if pending else ''})
        await engine.run_job(jid)
        rows=store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?',(jid,)).fetchall()
        assert len(rows)==2 and all(r['state']==('pending' if pending else 'skipped') for r in rows)
        assert all('P1/P2 分段文件' in r['message'] for r in rows)
        assert all(cloud.nodes[f['id']]['parent_id']==('300' if pending else '100') for f in files)
        assert not list(engine.root.rglob('*.strm'))
        await http.aclose();store.close()
    asyncio.run(run(False));asyncio.run(run(True))


def test_p1_marker_handles_uncertainty_and_ignores_dolby_profiles():
    from strimkeep.media import split_video_marker
    assert split_video_marker('无人区.1080p.P1.mkv')[1] == 1
    assert split_video_marker('无人区.1080p.P2.mkv')[1] == 2
    assert split_video_marker('无人区1080p1.mkv')[0] == split_video_marker('无人区1080p2.mkv')[0]
    assert split_video_marker('Movie.2160p.DoVi.P1.mkv') is None
    assert split_video_marker('Movie.2160p.DV.P2.mkv') is None
    assert split_video_marker('Movie.2160p.DoVi.P8.mkv') is None


def test_movie_p1_is_reported_in_preview_without_assuming_a_complete_movie():
    name='测试电影 (2024).1080p.P1.mkv'
    try:
        require_single_episode(parse_name(name),name)
    except NeedsReview as exc:
        assert 'P1/P2' in str(exc)
    else:
        raise AssertionError(name)


def test_chinese_movie_parts_require_matching_pair(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        for i,part in enumerate(('上','下')):
            cloud.add_video(str(1300+i),f'测试电影{part} (2024).1080p.mkv')
        jid=new_job(store,{**CONFIG,'pending_cid':''})
        await engine.run_job(jid)
        rows=store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?',(jid,)).fetchall()
        assert len(rows)==2 and all(r['state']=='skipped' and '上/下配对分段' in r['message'] for r in rows)
        assert all(cloud.nodes[str(1300+i)]['parent_id']=='100' for i in range(2))
        await http.aclose();store.close()
    asyncio.run(run())


def test_p1_in_title_before_year_is_not_split():
    from strimkeep.media import split_video_marker
    assert split_video_marker('P1 Show (2024).1080p.mkv') is None
