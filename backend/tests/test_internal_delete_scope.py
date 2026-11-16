"""深度删除不再有登记/窗口：两库目标都直接处理，删完不留羁绊。"""
from strimkeep import internal_delete
from strimkeep.deep_delete import drop_internal
from strimkeep.store import Store


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', 'test-password-1234')


def targets(path):
    return {'managed': [{'file_id': '1', 'relative_path': path}],
            'share': [{'id': 's1', 'strm_path': path}]}


def test_drop_internal_keeps_every_target(tmp_path):
    store = make_store(tmp_path)
    path = '媒体库/剧集/剧名 (2026) {tmdb-1}/Season 1/剧名.2026.S01E01.strm'
    internal_delete.remember(store, [path])
    filtered, dropped = drop_internal(store, targets(path))
    assert dropped == 0 and filtered['managed'] and filtered['share']
    store.close()


def test_share_markers_are_not_stored(tmp_path):
    store = make_store(tmp_path)
    path = '分享影视库/剧集/剧名 (2026) {tmdb-1}/Season 1/剧名.2026.S01E01.strm'
    internal_delete.remember(store, [path], 'share')
    assert internal_delete.recent(store, kind='share') == set()
    filtered, dropped = drop_internal(store, targets(path))
    assert dropped == 0 and filtered['share']
    store.close()
