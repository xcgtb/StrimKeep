"""Unidentified share STRMs retain work folders without sharing-code wrappers."""
import asyncio
import json

from strimkeep import share_service
from test_phase2_share_pipeline import share_setup


def test_unidentified_full_season_preserves_work_directory_and_individual_files(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        created = share_service.generate(store, engine.root, {'share_code':'codeA','receive_code':''}, [
            {'id':str(i), 'name':f'S01E{i:02d}.mkv', 'size':5000, 'path':'璀璨之城 (2026)/Season 1/'}
            for i in range(1, 4)
        ])['created']
        for entry in created:
            row = store.db.execute('SELECT * FROM share_strms WHERE id=?', (entry['id'],)).fetchone()
            await share_service._to_unident(engine, 1, row, None, '未识别', deterministic=True)
        result = store.db.execute('SELECT strm_path FROM share_strms ORDER BY file_id').fetchall()
        for i, row in enumerate(result, 1):
            assert row['strm_path'] == f'分享STRM未识别/璀璨之城 (2026)/Season 1/S01E{i:02d}.mkv.strm'
            assert (engine.root / row['strm_path']).is_file()
        assert not (engine.root / '分享STRM未识别' / 'codeA').exists()
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_unidentified_same_episode_from_two_shares_never_overwrites(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entries = []
        for code, fid in [('codeA','301'), ('codeB','302')]:
            result = share_service.generate(store, engine.root, {'share_code':code,'receive_code':''}, [
                {'id':fid, 'name':'S01E01.mkv', 'size':5000, 'path':'同一作品 (2024)/Season 1/'}])
            entries.append(result['created'][0])
        contents = {}
        for entry in entries:
            row = store.db.execute('SELECT * FROM share_strms WHERE id=?', (entry['id'],)).fetchone()
            contents[row['id']] = (engine.root / row['strm_path']).read_bytes()
            await share_service._to_unident(engine, 1, row, None, '未识别', deterministic=True)
        rows = store.db.execute('SELECT id,strm_path FROM share_strms ORDER BY file_id').fetchall()
        assert len({row['strm_path'] for row in rows}) == 2
        for row in rows:
            assert row['strm_path'].startswith('分享STRM未识别/同一作品 (2024)/Season 1/')
            assert (engine.root / row['strm_path']).read_bytes() == contents[row['id']]
        assert rows[0]['strm_path'].endswith('S01E01.mkv.strm')
        assert rows[1]['strm_path'].endswith('].strm')
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_unidentified_fallback_collision_preserves_source_and_records_actual_hold_target(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, {'share_code':'codeA','receive_code':''}, [
            {'id':'301', 'name':'S01E01.mkv', 'size':5000, 'path':'作品 (2024)/'}])['created'][0]
        row = store.db.execute('SELECT * FROM share_strms WHERE id=?', (entry['id'],)).fetchone()
        source = engine.root / row['strm_path']
        original = source.read_bytes()
        unident = engine.root / share_service.UNIDENT_NAME
        first, alternate = share_service._unidentified_targets(unident, row)
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b'existing-1'); alternate.write_bytes(b'existing-2')
        await share_service._to_unident(engine, 1, row, None, '未识别', deterministic=True)
        assert source.read_bytes() == original
        assert first.read_bytes() == b'existing-1' and alternate.read_bytes() == b'existing-2'
        assert store.db.execute('SELECT strm_path FROM share_strms WHERE id=?', (row['id'],)).fetchone()[0] == row['strm_path']
        hold = json.loads(store.meta('share-unident-hold:' + row['id']))
        assert hold['target_path'] == alternate.relative_to(unident).as_posix()
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_unidentified_root_and_legacy_paths_remain_compatible(tmp_path):
    root = tmp_path / 'unidentified'
    flat = {'id':'x', 'share_code':'abc', 'strm_path':'分享STRM待整理/03.mkv.strm'}
    legacy = {'id':'y', 'share_code':'abc', 'strm_path':'分享STRM待整理/old-layout/作品/03.mkv.strm'}
    assert share_service._unidentified_targets(root, flat)[0] == root / '03.mkv.strm'
    assert share_service._unidentified_targets(root, legacy)[0] == root / 'old-layout/作品/03.mkv.strm'


def test_mixed_episode_results_move_only_failed_file_and_preserve_neighbors(tmp_path, monkeypatch):
    from strimkeep.media import NeedsReview
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        created = share_service.generate(store, engine.root, {'share_code':'codeA','receive_code':''}, [
            {'id':'301', 'name':'S01E01.mkv', 'size':5000, 'path':'测试剧集 (2024)/Season 1/'},
            {'id':'302', 'name':'S01E02.mkv', 'size':5000, 'path':'测试剧集 (2024)/Season 1/'},
        ])['created']
        source_dir = (engine.root / created[0]['strm_path']).parent
        marker = source_dir / 'unrelated.txt'
        marker.write_text('keep')
        empty = source_dir.parent / 'other-empty'
        empty.mkdir()
        original_hint = share_service.share_recognition_hint
        def hint(row):
            if row['name'] == 'S01E02.mkv': raise NeedsReview('缺少作品线索')
            return original_hint(row)
        monkeypatch.setattr(share_service, 'share_recognition_hint', hint)
        jid = share_service.enqueue_organize(engine, origin='web')
        await engine.run_job(jid)
        found = {row['file_id']: dict(row) for row in store.db.execute('SELECT * FROM share_strms')}
        assert found['301']['strm_path'].startswith('分享影视库/')
        assert found['302']['strm_path'] == '分享STRM未识别/S01E02.mkv.strm'
        assert (engine.root / found['301']['strm_path']).is_file()
        assert (engine.root / found['302']['strm_path']).is_file()
        assert marker.read_text() == 'keep' and empty.is_dir()
        unident = engine.root / share_service.UNIDENT_NAME
        assert sorted(p.name for p in unident.rglob('*.strm')) == ['S01E02.mkv.strm']
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_unidentified_loose_and_movie_in_folder_move_individually(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        created = share_service.generate(store, engine.root, {'share_code':'codeA','receive_code':''}, [
            {'id':'301', 'name':'03.mkv', 'size':5000, 'path':''},
            {'id':'302', 'name':'unknown.mkv', 'size':5000, 'path':'电影原文件夹/'},
        ])['created']
        for entry in created:
            row = store.db.execute('SELECT * FROM share_strms WHERE id=?', (entry['id'],)).fetchone()
            await share_service._to_unident(engine, 1, row, None, '未识别', deterministic=True)
        rows = {row['file_id']:row['strm_path'] for row in store.db.execute('SELECT file_id,strm_path FROM share_strms')}
        assert rows['301'] == '分享STRM未识别/03.mkv.strm'
        assert rows['302'] == '分享STRM未识别/电影原文件夹/unknown.mkv.strm'
        assert all((engine.root / path).is_file() for path in rows.values())
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_all_unidentified_group_keeps_work_folder_not_category(tmp_path, monkeypatch):
    from strimkeep.media import NeedsReview
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        share_service.generate(store, engine.root, {'share_code':'codeA','receive_code':''}, [
            {'id':'301','name':'S01E01.mkv','size':5000,'path':'外语电影/作品 (2024)/Season 1/'},
            {'id':'302','name':'S01E02.mkv','size':5000,'path':'外语电影/作品 (2024)/Season 1/'},
        ])
        def fail(row): raise NeedsReview('未识别')
        monkeypatch.setattr(share_service, 'share_recognition_hint', fail)
        jid = share_service.enqueue_organize(engine, origin='web')
        await engine.run_job(jid)
        paths = [r['strm_path'] for r in store.db.execute('SELECT strm_path FROM share_strms ORDER BY file_id')]
        assert paths == [f'分享STRM未识别/作品 (2024)/Season 1/S01E{i:02d}.mkv.strm' for i in (1,2)]
        assert not (engine.root / '分享STRM未识别' / '外语电影').exists()
        assert all((engine.root / path).is_file() for path in paths)
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_uncertain_interrupted_group_flattens_individual_failures(tmp_path, monkeypatch):
    from strimkeep.media import NeedsReview
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, {'share_code':'codeA','receive_code':''}, [
            {'id':'301','name':'S01E01.mkv','size':5000,'path':'作品 (2024)/Season 1/'},
        ])['created'][0]
        row = store.db.execute('SELECT * FROM share_strms WHERE id=?',(entry['id'],)).fetchone()
        layout = share_service._share_work_layout(row)
        store.set_meta(share_service._share_layout_marker(layout[0]), 'interrupted')
        def fail(row): raise NeedsReview('未识别')
        monkeypatch.setattr(share_service, 'share_recognition_hint', fail)
        jid = share_service.enqueue_organize(engine, origin='web')
        await engine.run_job(jid)
        result = store.db.execute('SELECT strm_path FROM share_strms WHERE id=?',(entry['id'],)).fetchone()[0]
        assert result == '分享STRM未识别/S01E01.mkv.strm'
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_unregistered_new_member_prevents_assuming_full_failure(tmp_path, monkeypatch):
    from strimkeep.media import NeedsReview
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, {'share_code':'codeA','receive_code':''}, [
            {'id':'301','name':'S01E01.mkv','size':5000,'path':'作品 (2024)/Season 1/'},
        ])['created'][0]
        unknown = (engine.root / entry['strm_path']).parent / 'unregistered.strm'
        unknown.write_text('keep')
        def fail(row): raise NeedsReview('未识别')
        monkeypatch.setattr(share_service, 'share_recognition_hint', fail)
        jid = share_service.enqueue_organize(engine, origin='web')
        await engine.run_job(jid)
        result = store.db.execute('SELECT strm_path FROM share_strms WHERE id=?',(entry['id'],)).fetchone()[0]
        assert result == '分享STRM未识别/S01E01.mkv.strm'
        assert unknown.read_text() == 'keep'
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_pending_new_share_preserves_work_without_share_code_and_distinguishes_sources(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entries = []
        for code, file_id in [('codeA','301'), ('codeB','302')]:
            result = share_service.generate(store, engine.root, {'share_code':code,'receive_code':''}, [
                {'id':file_id,'name':'S01E01.mkv','size':5000,'path':'作品 (2024)/Season 1/'}])
            entries.append(result['created'][0])
        rows = [store.db.execute('SELECT * FROM share_strms WHERE id=?',(entry['id'],)).fetchone() for entry in entries]
        assert len({r['strm_path'] for r in rows}) == 2
        assert all(r['strm_path'].startswith('分享STRM待整理/作品 (2024)/Season 1/') for r in rows)
        assert not (engine.root / '分享STRM待整理/codeA').exists()
        for r in rows:
            assert share_service.share_recognition_hint(r)['query'] == '作品'
        assert share_service._share_work_layout(rows[0])[0] != share_service._share_work_layout(rows[1])[0]
        assert all((engine.root / r['strm_path']).is_file() for r in rows)
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_later_failed_episode_of_accepted_group_is_flat(tmp_path, monkeypatch):
    from strimkeep.media import NeedsReview
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        secret={'share_code':'codeA','receive_code':''}
        share_service.generate(store, engine.root, secret, [
            {'id':'301','name':'S01E01.mkv','size':5000,'path':'测试剧集 (2024)/Season 1/'}])
        jid=share_service.enqueue_organize(engine,origin='web')
        await engine.run_job(jid)
        assert store.db.execute('SELECT strm_path FROM share_strms WHERE file_id=?', ('301',)).fetchone()[0].startswith('分享影视库/')
        share_service.generate(store, engine.root, secret, [
            {'id':'302','name':'S01E02.mkv','size':5000,'path':'测试剧集 (2024)/Season 1/'}])
        def fail(row): raise NeedsReview('未识别')
        monkeypatch.setattr(share_service, 'share_recognition_hint', fail)
        jid=share_service.enqueue_organize(engine,origin='web')
        await engine.run_job(jid)
        assert store.db.execute('SELECT strm_path FROM share_strms WHERE file_id=?', ('302',)).fetchone()[0] == '分享STRM未识别/S01E02.mkv.strm'
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        await http.aclose(); store.close()
    asyncio.run(scenario())
