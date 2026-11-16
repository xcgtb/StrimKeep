"""深度删除无状态回归：程序清理本地文件不再写登记，也不会挡住后续删除。

历史事故靠「登记 + 跳过」保护；现在改为删除即删除、清除台账，云端真删仍靠
output_registry / owned_outputs / 文件身份核验，不依赖任何时间窗口。
"""
import json

from strimkeep.fsops import purge_strm
from strimkeep.library_roots import apply_rename, prune_legacy
from strimkeep.store import Store

PASSWORD = 'test-password-1234'
ARCHIVE = '3547068638915724608'
CLOUD_LABEL = '115 网盘 / 媒体库'
NEW = '媒体库'


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', PASSWORD)


def test_purge_strm_removes_file(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    target = root / NEW / '电影' / 'x' / 'a.strm'
    target.parent.mkdir(parents=True)
    target.write_text('http://play.example/play115/pc/1/' + 'A' * 40 + '/1/a.mkv', encoding='utf-8')
    purge_strm(target, root)
    assert not target.exists()
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
    store.close()


def test_rename_moves_without_marker(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    old, new = '旧库', '新库'
    season = root / old / '剧集' / '剧/Season 1'
    season.mkdir(parents=True)
    (season / 'a.strm').write_text('x', encoding='utf-8')
    result = apply_rename(store, root, old, new, dry_run=False)
    assert result['status'] == 'applied'
    store.close()
