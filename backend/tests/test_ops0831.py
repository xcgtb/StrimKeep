"""0.8.31 回归：本地多余 STRM 的清理（按文件身份判定）+ 旧命名副本收敛。

背景：`sweep_orphans()` 原来只认 `/play/<token>` 形态，而 0.7.12 起 STRM 写的是
`/play115/<pickcode>/<fid>/<sha1>/<size>/<name>`，于是「本地多出来的旧命名副本」
再也没被清理过（现场累积 395 个）。这里固定三条口径：
1. play115 副本：同一 file_id 已被登记到别的、且仍存在的路径 → 删；
2. 查不到登记、或这一集没有登记 → 只报数，不猜（可能是唯一来源）；
3. 旧形态 `/play/<token>`：播放身份不存在或已撤销 → 照旧删。
"""
import asyncio
import json

from test_organize import new_job, setup
from test_phase5 import sync_config

from strimkeep.library_roots import legacy_report, prune_legacy, restore_legacy
from strimkeep.store import Store

PASSWORD = 'test-password-1234'
ARCHIVE = '3547068638915724608'
CLOUD_LABEL = '115 网盘 / 媒体库'
NEW = '媒体库'


async def _registered_output(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    video = cloud.add_video(name='Movie.2024.1080p.mkv')
    video['sha1'] = 'A' * 40
    job = new_job(store, sync_config())
    await engine.run_job(job)
    row = store.db.execute('SELECT * FROM output_registry LIMIT 1').fetchone()
    assert row is not None and (engine.root / row['relative_path']).is_file()
    return store, cloud, http, engine, job, row


def test_sweep_orphans_deletes_play115_duplicate(tmp_path):
    async def run():
        store, cloud, http, engine, job, row = await _registered_output(tmp_path)
        canonical = row['relative_path']
        original = engine.root / canonical
        legacy = canonical.replace('Movie.2024.1080p', 'Movie.2024.初潮')
        legacy_path = engine.root / legacy
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_bytes(original.read_bytes())

        config = json.loads(engine.job(job)['config'])
        registered = {canonical} | set(json.loads(row['files']))
        removed, kept = engine.sync.sweep_orphans(job, config, registered)

        assert removed == 1 and kept == 0
        assert not legacy_path.exists(), '同一云端文件的旧命名副本应被清掉'
        assert original.is_file(), '登记的正式输出不能被删'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_orphans_keeps_unknown_and_uncovered_files(tmp_path):
    async def run():
        store, cloud, http, engine, job, row = await _registered_output(tmp_path)
        config = json.loads(engine.job(job)['config'])
        base = (config.get('playback_base') or '').rstrip('/')
        unknown = engine.root / NEW / '电影' / '未登记' / 'x.strm'
        unknown.parent.mkdir(parents=True, exist_ok=True)
        unknown.write_text(base + '/play115/pc/999999/' + 'B' * 40 + '/1024/x.mkv', encoding='utf-8')

        removed, kept = engine.sync.sweep_orphans(job, config,
                                                  {row['relative_path']} | set(json.loads(row['files'])))
        assert removed == 0 and kept == 1
        assert unknown.is_file(), '查不到登记的 STRM 只能报数，不能删'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_orphans_still_cleans_legacy_play_tokens(tmp_path):
    async def run():
        store, cloud, http, engine, job, row = await _registered_output(tmp_path)
        config = json.loads(engine.job(job)['config'])
        base = (config.get('playback_base') or '').rstrip('/')
        dead = engine.root / NEW / '电影' / '旧形态' / 'old.strm'
        dead.parent.mkdir(parents=True, exist_ok=True)
        dead.write_text(base + '/play/不存在的身份/video.mkv', encoding='utf-8')

        removed, kept = engine.sync.sweep_orphans(job, config,
                                                  {row['relative_path']} | set(json.loads(row['files'])))
        assert removed == 1 and kept == 0 and not dead.exists()
        await http.aclose()
        store.close()
    asyncio.run(run())



def _tree(tmp_path):
    root = tmp_path / 'strm'
    season = root / NEW / '剧集' / '剧 (2024) {tmdb-1}' / 'Season 1'
    season.mkdir(parents=True)
    files = {
        'e01': season / '剧.2024.S01E01.第1集.strm',
        'e02': season / '剧.2024.S01E02.第2集.strm',
        'dup': season / '剧.2024.S01E01.初潮.strm',
        'gap': season / '剧.2024.S01E03.第3集.strm',
        'only': root / NEW / '电影' / '只有旧命名 (2024) {tmdb-2}' / '片.2024.strm',
    }
    for path in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://play.example/play115/pc/fid/' + 'C' * 40 + '/1/x.mkv', encoding='utf-8')
    return root, files


def test_prune_legacy_only_deletes_covered_episodes(tmp_path):
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    root, files = _tree(tmp_path)
    store.set_many({'organize_config': {'archive_cid': ARCHIVE, 'library_dir_name': '',
                                        'directory_labels': {ARCHIVE: CLOUD_LABEL}}})
    with store.db:
        for index, key in enumerate(('e01', 'e02'), 1):
            store.db.execute(
                "INSERT INTO organized_files(file_id,job_id,item_id,playback_id,relative_path,metadata)"
                " VALUES(?,1,?,'p',?,'{}')",
                (str(index), index, files[key].relative_to(root).as_posix()))

    report = legacy_report(store, root)
    deletable = {entry['relative_path'] for entry in report['deletable']}
    keep = {entry['relative_path'] for entry in report['keep']}
    assert deletable == {files['dup'].relative_to(root).as_posix()}
    assert files['gap'].relative_to(root).as_posix() in keep, '这一集没有登记 → 只能保留'
    assert files['only'].relative_to(root).as_posix() in keep, '整部只有旧命名 → 只能保留'

    dry = prune_legacy(store, root)
    assert dry['status'] == 'dry_run' and dry['deletable'] == 1 and files['dup'].is_file()

    done = prune_legacy(store, root, apply=True)
    assert done['status'] == 'applied' and done['removed'] == 1
    assert not files['dup'].exists() and files['e01'].is_file() and files['gap'].is_file()

    back = restore_legacy(store, root, done['manifest'])
    assert back['restored'] == 1 and files['dup'].is_file()
    store.close()
