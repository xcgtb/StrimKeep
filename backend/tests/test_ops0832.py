"""0.8.32 回归：程序自己删本地 STRM 时，深度删除不能再删云端源文件。

现场事故（2026-11-04）：`--prune-legacy` 删掉 395 个旧命名副本 → Emby 报「Episode deleted」
→ 深度删除按台账命中，删了 61 个云端源文件（约 610 GB，回收站可还原）。

三条口径：
1. 程序删本地 STRM 前先登记（`internal_delete.remember`）；
2. 深度删除匹配到这些路径时只跳过，绝不删云端（`deep_delete.drop_internal`）；
3. 深删是真删模式时，`--prune-legacy` 拒绝执行。
"""
import json
import time

from strimkeep import internal_delete
from strimkeep.deep_delete import drop_internal
from strimkeep.fsops import purge_strm
from strimkeep.library_roots import prune_legacy
from strimkeep.store import Store

PASSWORD = 'test-password-1234'
ARCHIVE = '3547068638915724608'
CLOUD_LABEL = '115 网盘 / 媒体库'
NEW = '媒体库'


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', PASSWORD)


def test_internal_delete_window_remembers_and_expires(tmp_path):
    store = make_store(tmp_path)
    internal_delete.remember(store, ['媒体库/电影/a.strm', '媒体库/电影/b.strm'])
    assert internal_delete.recent(store) == {'媒体库/电影/a.strm', '媒体库/电影/b.strm'}
    assert internal_delete.recent(store, window=0) == set(), '窗口过期后不应再算内部删除'
    store.close()


def test_drop_internal_removes_program_deletions_only(tmp_path):
    store = make_store(tmp_path)
    internal_delete.remember(store, ['媒体库/电影/a.strm'])
    targets = {'managed': [{'file_id': '1', 'relative_path': '媒体库/电影/a.strm'},
                           {'file_id': '2', 'relative_path': '媒体库/电影/b.strm'}],
               'share': [{'id': 's1', 'strm_path': '分享影视库/电影/c.strm'}]}
    filtered, dropped = drop_internal(store, targets)
    assert dropped == 1
    assert [t['file_id'] for t in filtered['managed']] == ['2']
    assert len(filtered['share']) == 1
    store.close()


def test_purge_strm_marks_internal_deletion(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    target = root / NEW / '电影' / 'x' / 'a.strm'
    target.parent.mkdir(parents=True)
    target.write_text('http://play.example/play115/pc/1/' + 'A' * 40 + '/1/a.mkv', encoding='utf-8')

    purge_strm(target, root, store=store)

    assert not target.exists()
    assert '媒体库/电影/x/a.strm' in internal_delete.recent(store)
    store.close()


def test_prune_legacy_refuses_when_deep_delete_is_live(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    season = root / NEW / '剧集' / '剧 (2024) {tmdb-1}' / 'Season 1'
    season.mkdir(parents=True)
    kept = season / '剧.2024.S01E01.第1集.strm'
    dup = season / '剧.2024.S01E01.初潮.strm'
    for path in (kept, dup):
        path.write_text('http://play.example/play115/pc/1/' + 'B' * 40 + '/1/x.mkv', encoding='utf-8')
    store.set_many({'organize_config': {'archive_cid': ARCHIVE, 'library_dir_name': '',
                                        'directory_labels': {ARCHIVE: CLOUD_LABEL}},
                    'sync_config': {'deep_delete_dry_run': False}})
    with store.db:
        store.db.execute("INSERT INTO organized_files(file_id,job_id,item_id,playback_id,relative_path,metadata)"
                         " VALUES('1',1,1,'p',?,'{}')", (kept.relative_to(root).as_posix(),))

    blocked = prune_legacy(store, root, apply=True)
    assert blocked['status'] == 'blocked' and '真删' in blocked['reason']
    assert dup.is_file(), '深删是真删模式时必须整体拒绝，一个文件都不能删'

    store.set_many({'sync_config': {'deep_delete_dry_run': True}})
    done = prune_legacy(store, root, apply=True)
    assert done['status'] == 'applied' and done['removed'] == 1 and not dup.exists()
    assert dup.relative_to(root).as_posix() in internal_delete.recent(store), '删除前应登记，供深删跳过'
    store.close()


def test_rename_marks_moved_paths(tmp_path):
    """改名同样会让 Emby 报旧路径删除，所以也要登记。"""
    from strimkeep.library_roots import apply_rename
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    old, new = '旧库', '新库'
    season = root / old / '剧集' / '剧/Season 1'
    season.mkdir(parents=True)
    (season / 'a.strm').write_text('x', encoding='utf-8')
    result = apply_rename(store, root, old, new, dry_run=False)
    assert result['status'] == 'applied'
    assert '旧库/剧集/剧/Season 1/a.strm' in internal_delete.recent(store)
    store.close()
