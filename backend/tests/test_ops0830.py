"""0.8.30 回归：STRM 库根镜像 115 目录名、库根改名可回滚、库根不一致自检。

背景（2026-11 现场）：115 上的归档目录和 STRM 同步根都叫「媒体库」，而配置里的
`library_dir_name` 是「影视媒体库」。整理/同步按配置把**台账**改到「影视媒体库」，
文件却留在「媒体库」，978 行台账里 924 行在磁盘上找不到，Emby 只能收录其中一边。
"""
import json

import pytest

from strimkeep.fsops import purge_strm
from strimkeep.library_roots import apply_rename, inspect_state, rollback, startup_check, rewrite
from strimkeep.organize_options import (DEFAULT_LIBRARY_DIR_NAME, cloud_dir_name, library_root,
                                        options)
from strimkeep.store import Store

PASSWORD = 'test-password-1234'
ARCHIVE = '3547068638915724608'
CLOUD_LABEL = '115 网盘 / 媒体库'
OLD, NEW = '影视媒体库', '媒体库'


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', PASSWORD)


# ------------------------------------------------- 1) 库根名从哪来

def test_cloud_dir_name_takes_the_last_segment():
    assert cloud_dir_name(CLOUD_LABEL) == '媒体库'
    assert cloud_dir_name('a/b/媒体库') == '媒体库'
    assert cloud_dir_name(' 媒体库 ') == '媒体库'
    assert cloud_dir_name('') == ''


def test_library_root_mirrors_the_cloud_directory_name():
    config = {'archive_cid': ARCHIVE, 'directory_labels': {ARCHIVE: CLOUD_LABEL}}
    assert library_root(config) == '媒体库'
    # 扫描到的真实路径比标签更权威
    path = [{'id': 'r', 'name': '115 网盘'}, {'id': ARCHIVE, 'name': '媒体库2026'}]
    assert library_root(config, path) == '媒体库2026'
    # 显式配置优先（老配置、或本地想用别的名字）
    assert library_root({**config, 'library_dir_name': '自定义库'}) == '自定义库'
    # 推不出来才用兜底值
    assert library_root({}) == DEFAULT_LIBRARY_DIR_NAME
    assert library_root({'archive_cid': '9'}) == DEFAULT_LIBRARY_DIR_NAME


def test_organize_options_allows_empty_library_name():
    assert options({})['library_dir_name'] == ''
    assert options({'library_dir_name': ''})['library_dir_name'] == ''
    assert options({'library_dir_name': ' 媒体库 '})['library_dir_name'] == '媒体库'
    with pytest.raises(ValueError):
        options({'library_dir_name': 'a/b'})


def test_sync_relative_mirrors_the_cloud_directory_name():
    from strimkeep.sync import Sync
    config = {'archive_cid': ARCHIVE, 'library_dir_name': ''}
    source = {'name': 'A.mkv', '_root': 'r',
              '_path': [{'id': 'r', 'name': '115 网盘'}, {'id': ARCHIVE, 'name': NEW},
                        {'id': 'x', 'name': '剧集'}]}
    assert Sync.relative(object(), source, config) == NEW + '/剧集/A.strm'
    # 显式配置仍旧生效
    assert Sync.relative(object(), source, {**config, 'library_dir_name': '旧库'}) == '旧库/剧集/A.strm'


def test_rewrite_handles_prefixes_and_nested_json():
    assert rewrite('影视媒体库/电影/a.strm', OLD, NEW) == '媒体库/电影/a.strm'
    assert rewrite('/media/影视媒体库/电影', OLD, NEW) == '/media/媒体库/电影'
    assert rewrite('影视媒体库2/x', OLD, NEW) == '影视媒体库2/x'
    assert rewrite({'影视媒体库/a': ['影视媒体库/b']}, OLD, NEW) == {'媒体库/a': ['媒体库/b']}



# ------------------------------------------------- 2) 库根改名：移文件 + 改台账 + 可回滚

def _seed(store, relative, nfo_relative):
    with store.db:
        store.db.execute("INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,revoked,created)"
                         " VALUES('p1','f1','pc','A.mkv',?,'h',0,0)", (relative,))
        store.db.execute("INSERT INTO organized_files(file_id,job_id,item_id,playback_id,relative_path,metadata)"
                         " VALUES('f1',1,1,'p1',?,'{}')", (relative,))
        store.db.execute("INSERT INTO owned_outputs(relative_path,account_id,file_id,digest)"
                         " VALUES(?,'acc','f1','d1')", (relative,))
        store.db.execute("INSERT INTO output_registry(account_id,file_id,source,trail,playback_id,"
                         "token_cipher,relative_path,files,active,updated)"
                         " VALUES('acc','f1','{}','[]','p1','c',?,?,1,0)",
                         (relative, json.dumps({relative: 'd1', nfo_relative: 'd2'})))
        store.db.execute("INSERT INTO organize_items(job_id,file_id,original,plan) VALUES(1,'f1','{}',?)",
                         (json.dumps({'relative_path': relative, 'local_files': {relative: 'd1'}}),))
        store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('completed',?,0,0)",
                         (json.dumps({'job_kind': 'organize', 'library_dir_name': OLD,
                                      'parts': ['剧集']}),))


def _make_tree(root):
    season = root / OLD / '剧集' / '剧目 (2024) {tmdb-1}' / 'Season 1'
    season.mkdir(parents=True)
    (season / 'A.S01E01.strm').write_text('http://x/play115/a', encoding='utf-8')
    (season / 'A.S01E01.nfo').write_text('<movie/>', encoding='utf-8')
    relative = OLD + '/剧集/剧目 (2024) {tmdb-1}/Season 1/A.S01E01.strm'
    return season, relative


def test_rename_moves_files_and_rewrites_every_path_column(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    season, relative = _make_tree(root)
    _seed(store, relative, relative.replace('.strm', '.nfo'))

    dry = apply_rename(store, root, OLD, NEW)
    assert dry['status'] == 'dry_run' and dry['moves'] == 2 and dry['rows']['playbacks'] == 1
    assert (season / 'A.S01E01.strm').is_file()          # 演练不动文件

    result = apply_rename(store, root, OLD, NEW, dry_run=False)
    assert result['status'] == 'applied' and result['moved'] == 2
    expected = NEW + '/剧集/剧目 (2024) {tmdb-1}/Season 1/A.S01E01.strm'
    assert (root / expected).is_file()
    assert not (root / OLD).exists()                      # 旧库根被移空后清掉
    assert store.db.execute('SELECT relative_path FROM playbacks').fetchone()[0] == expected
    assert store.db.execute('SELECT relative_path FROM organized_files').fetchone()[0] == expected
    assert store.db.execute('SELECT relative_path FROM owned_outputs').fetchone()[0] == expected
    registry = store.db.execute('SELECT relative_path,files FROM output_registry').fetchone()
    assert registry['relative_path'] == expected
    assert set(json.loads(registry['files'])) == {expected, expected.replace('.strm', '.nfo')}
    plan = json.loads(store.db.execute('SELECT plan FROM organize_items').fetchone()[0])
    assert plan['relative_path'] == expected and set(plan['local_files']) == {expected}
    job = json.loads(store.db.execute('SELECT config FROM organize_jobs').fetchone()[0])
    assert job['library_dir_name'] == NEW                 # 任务快照里的库根也一起改
    assert store.meta('library_root_migration')
    store.close()


def test_rename_refuses_to_overwrite_existing_files(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    season, relative = _make_tree(root)
    _seed(store, relative, relative.replace('.strm', '.nfo'))
    target = root / NEW / '剧集/剧目 (2024) {tmdb-1}/Season 1/A.S01E01.strm'
    target.parent.mkdir(parents=True)
    target.write_text('keep-me', encoding='utf-8')

    result = apply_rename(store, root, OLD, NEW, dry_run=False)
    assert result['status'] == 'blocked'
    assert result['collisions'] == ['剧集/剧目 (2024) {tmdb-1}/Season 1/A.S01E01.strm']
    assert target.read_text(encoding='utf-8') == 'keep-me'
    assert (season / 'A.S01E01.strm').is_file()            # 没搬任何东西
    assert store.db.execute('SELECT relative_path FROM playbacks').fetchone()[0] == relative
    store.close()


def test_rollback_restores_files_and_paths(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    _season, relative = _make_tree(root)
    _seed(store, relative, relative.replace('.strm', '.nfo'))
    apply_rename(store, root, OLD, NEW, dry_run=False)

    result = rollback(store, root)
    assert result['status'] == 'rolled_back' and result['moved'] == 2
    assert (root / relative).is_file()
    assert not (root / NEW).exists()
    assert store.db.execute('SELECT relative_path FROM playbacks').fetchone()[0] == relative
    config = json.loads(store.db.execute('SELECT config FROM organize_jobs').fetchone()[0])
    assert config['library_dir_name'] == OLD
    assert store.meta('library_root_migration') in (None, '')
    store.close()


# ------------------------------------------------- 3) 启动自检与空目录清理边界

def test_startup_check_warns_once_and_clears_when_consistent(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    (root / NEW / '剧集').mkdir(parents=True)
    (root / NEW / '剧集' / 'a.strm').write_text('x', encoding='utf-8')

    store.set_many({'organize_config': {'archive_cid': ARCHIVE, 'library_dir_name': OLD}})
    assert startup_check(store, root)                       # 配置写的是旧名 → 报警
    count = store.db.execute("SELECT count(*) FROM events WHERE message LIKE '%库根不一致%'").fetchone()[0]
    assert count == 1
    assert startup_check(store, root) is None               # 同一签名不重复刷
    assert store.db.execute("SELECT count(*) FROM events WHERE message LIKE '%库根不一致%'").fetchone()[0] == 1

    store.set_many({'organize_config': {'archive_cid': ARCHIVE, 'library_dir_name': '',
                                        'directory_labels': {ARCHIVE: CLOUD_LABEL}}})
    assert startup_check(store, root) is None               # 跟随 115 目录名 → 一致
    assert store.meta('library_root_notice') in (None, '')
    store.close()


def test_inspect_state_reports_config_ledger_and_disk(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    (root / NEW / '电影').mkdir(parents=True)
    (root / NEW / '电影' / 'a.strm').write_text('x', encoding='utf-8')
    (root / OLD / '电影').mkdir(parents=True)
    (root / OLD / '电影' / 'b.strm').write_text('y', encoding='utf-8')
    store.set_many({'organize_config': {'archive_cid': ARCHIVE, 'library_dir_name': '',
                                        'directory_labels': {ARCHIVE: CLOUD_LABEL}}})
    with store.db:
        store.db.execute("INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,revoked,created)"
                         " VALUES('p1','f1','pc','b.mkv',?,'h',0,0)", (OLD + '/电影/b.strm',))
    state = inspect_state(store, root)
    assert state['expected_root'] == NEW
    assert {entry['name']: entry['strm'] for entry in state['disk']} == {NEW: 1, OLD: 1}
    assert state['ledger']['playbacks'] == {OLD: 1}
    store.close()


def test_purge_strm_never_removes_the_library_root(tmp_path):
    root = tmp_path / 'strm'
    season = root / NEW / '剧集' / '剧 (2024)' / 'Season 1'
    season.mkdir(parents=True)
    strm = season / '剧.S01E01.strm'
    strm.write_text('x', encoding='utf-8')
    (season / '剧.S01E01.nfo').write_text('n', encoding='utf-8')

    store = make_store(tmp_path)
    result = purge_strm(strm, root, store=store)
    store.close()
    assert result['file'] == 1 and not season.exists()
    assert (root / NEW).is_dir()                            # 一级目录（库根）永远保留


def test_purge_strm_scopes_sidecars_to_a_single_episode(tmp_path):
    """单集删除只清本集同名侧车，不动同季其它集与目录级海报。"""
    root = tmp_path / 'strm'
    season = root / NEW / '剧集' / '剧 (2024)' / 'Season 1'
    season.mkdir(parents=True)
    e1 = season / '剧.S01E01.strm'
    e1.write_text('x', encoding='utf-8')
    (season / '剧.S01E01.nfo').write_text('n', encoding='utf-8')
    (season / '剧.S01E01-media-info.json').write_text('{}', encoding='utf-8')
    e2 = season / '剧.S01E02.strm'
    e2.write_text('x', encoding='utf-8')
    (season / '剧.S01E02.nfo').write_text('n', encoding='utf-8')
    (season / 'poster.jpg').write_text('p', encoding='utf-8')

    store = make_store(tmp_path)
    result = purge_strm(e1, root, store=store)
    store.close()

    assert result['file'] == 1 and result['sidecars'] == 2 and result['dirs'] == 0
    assert not e1.exists()
    assert not (season / '剧.S01E01.nfo').exists()
    assert not (season / '剧.S01E01-media-info.json').exists()
    # 其它集和目录级海报不受影响
    assert e2.exists() and (season / '剧.S01E02.nfo').exists()
    assert (season / 'poster.jpg').exists()
    assert season.is_dir()


def test_purge_strm_clears_directory_sidecars_on_last_episode(tmp_path):
    """删掉目录里最后一集后，目录级侧车与空目录一起清。"""
    root = tmp_path / 'strm'
    season = root / NEW / '剧集' / '剧 (2024)' / 'Season 1'
    season.mkdir(parents=True)
    e1 = season / '剧.S01E01.strm'
    e1.write_text('x', encoding='utf-8')
    (season / '剧.S01E01.nfo').write_text('n', encoding='utf-8')
    (season / 'poster.jpg').write_text('p', encoding='utf-8')
    (season / 'tvshow.nfo').write_text('t', encoding='utf-8')

    store = make_store(tmp_path)
    result = purge_strm(e1, root, store=store)
    store.close()

    assert result['file'] == 1 and result['sidecars'] == 3
    assert not season.exists()
    assert (root / NEW).is_dir()


# ------------------------------------------------- 4) CLI：只改库根名，不丢其它配置

def test_set_config_keeps_other_organize_fields(tmp_path):
    """0.8.30 现场踩过：--set-config 用 options() 重建配置，把 archive_cid 等键整体丢了。"""
    from strimkeep.library_roots import main
    root = tmp_path / 'strm'
    (root / NEW).mkdir(parents=True)
    seed = Store(tmp_path / 'data', 'admin', PASSWORD)
    seed.set_many({'organize_config': {'archive_cid': ARCHIVE, 'source_cids': ['1'],
                                       'directory_labels': {ARCHIVE: CLOUD_LABEL},
                                       'notify_emby': True, 'library_dir_name': OLD}})
    seed.close()

    assert main(['--set-config', '', '--data', str(tmp_path / 'data'), '--strm', str(root)]) == 0

    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    config = store.get('organize_config')
    assert config['library_dir_name'] == ''
    assert config['archive_cid'] == ARCHIVE
    assert config['source_cids'] == ['1']
    assert config['directory_labels'] == {ARCHIVE: CLOUD_LABEL}
    assert config['notify_emby'] is True
    store.close()

# ------------------------------------------------- 5) 洗版后的空目录清理跟随库根

def test_wash_cleanup_follows_the_configured_library_root(tmp_path):
    """cleanup.local() 原来写死只清「影视媒体库」下的空目录；库根改成「媒体库」后就不清了。"""
    from strimkeep.cleanup import EmptyDirectoryCleanup
    root = tmp_path / 'strm'
    season = root / NEW / '剧集' / '作品 (2024)' / 'Season 1'
    season.mkdir(parents=True)
    relative = NEW + '/剧集/作品 (2024)/Season 1/E01.strm'
    row = {'state': 'done', 'plan': json.dumps({'relative_path': relative,
                                                'wash': {'old': [{'local_files': {relative: 'd1'}}]}})}

    class FakeStore:
        def get(self, key, default=None):
            if key == 'organize_config':
                return {'archive_cid': ARCHIVE, 'library_dir_name': '',
                        'directory_labels': {ARCHIVE: CLOUD_LABEL}}
            return default

    class Engine:
        root = tmp_path / 'strm'
        store = FakeStore()

    EmptyDirectoryCleanup(Engine()).local([row])
    assert not season.exists()                                   # 季度目录被清掉
    assert (root / NEW).is_dir()                                 # 库根不动
    assert (root / NEW / '剧集' / '作品 (2024)').is_dir()          # 只清到「≥4 段」深度（原有规则）

