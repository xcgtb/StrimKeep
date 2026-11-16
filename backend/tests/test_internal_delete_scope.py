"""内部删除登记按来源分域：媒体库的清理记录不能挡住分享库的深度清理。"""
import time

from strimkeep import internal_delete
from strimkeep.deep_delete import drop_internal
from strimkeep.fsops import purge_strm
from strimkeep.store import Store


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', 'test-password-1234')


def targets(path):
    return {'managed': [{'file_id': '1', 'relative_path': path}],
            'share': [{'id': 's1', 'strm_path': path}]}


def test_legacy_marker_only_protects_managed_outputs(tmp_path):
    """旧版不分域的登记按受管处理；否则分享侧永远清不掉（现场问题）。"""
    store = make_store(tmp_path)
    path = '分享影视库/剧集/国产剧集/剧名 (2026) {tmdb-1}/Season 1/剧名.2026.S01E01.strm'
    store.set_meta(internal_delete.KEY, {path: time.time()})
    filtered, dropped = drop_internal(store, targets(path))
    assert dropped == 1
    assert not filtered['managed']
    # 同路径的分享目标必须保留下来，供深度清理撤销分享入口
    assert filtered['share']
    store.close()


def test_share_marker_does_not_suppress_managed_and_vice_versa(tmp_path):
    store = make_store(tmp_path)
    path = '媒体库/剧集/剧名 (2026) {tmdb-1}/Season 1/剧名.2026.S01E01.strm'
    internal_delete.remember(store, [path], 'share')
    filtered, dropped = drop_internal(store, targets(path))
    assert dropped == 1 and filtered['managed'] and not filtered['share']
    internal_delete.remember(store, [path], 'managed')
    filtered, dropped = drop_internal(store, targets(path))
    assert dropped == 2 and not filtered['managed'] and not filtered['share']
    store.close()


def test_forget_clears_only_the_named_scope(tmp_path):
    store = make_store(tmp_path)
    path = '分享影视库/剧集/剧名 (2026) {tmdb-1}/Season 1/剧名.2026.S01E01.strm'
    internal_delete.remember(store, [path], 'share')
    internal_delete.remember(store, [path], 'managed')
    assert internal_delete.forget(store, [path], 'share') == 1
    assert path not in internal_delete.recent(store, kind='share')
    assert path in internal_delete.recent(store, kind='managed')
    store.close()


def test_purge_share_strm_records_share_scope(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    relative = '分享影视库/电影/某片 (2026) {tmdb-1}/某片.2026.strm'
    local = root / relative
    local.parent.mkdir(parents=True)
    local.write_text('http://play.example/play115/share/s1/' + 'A' * 40 + '/1/a.mkv', encoding='utf-8')
    purge_strm(local, root, store=store, kind='share')
    assert not local.exists()
    assert relative in internal_delete.recent(store, kind='share')
    assert relative not in internal_delete.recent(store, kind='managed')
    store.close()
