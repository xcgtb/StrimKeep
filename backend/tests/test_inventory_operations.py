import pytest
from strimkeep.inventory_operations import dual_actions, manual_actions, single_actions


@pytest.fixture
def projection():
    return {'complete': True, 'items': [{'kind': 'tv', 'tmdb_id': 1,
        'cloud': [{'id': '1', 'path': 'c/1', 'season': 1}, {'id': '2', 'path': 'c/2', 'season': 2}],
        'share': [{'id': '3', 'path': 's/1', 'season': 1}]}]}


def test_manual_selection_is_server_side_and_both_libraries(projection):
    selection = [{'kind': 'tv', 'tmdb_id': 1, 'side': 'both'}]
    assert len(manual_actions(projection, selection)) == 3
    with pytest.raises(ValueError): manual_actions(projection, selection * 2)
    # 库存「完整」不再是全局闸：计划由快照 + 逐作品核对把关。
    assert len(manual_actions({**projection, 'complete': False}, selection)) == 3


def test_dual_selection_only_losing_side_of_selected_season(projection):
    preview = {'scan_complete': True, 'dual': [{'kind': 'tv', 'tmdb_id': 1, 'season': 1, 'winner': 'share'}]}
    selection = [{'kind': 'tv', 'tmdb_id': 1, 'season': 1, 'winner': 'share'}]
    assert dual_actions(projection, preview, selection) == [{'side': 'cloud', 'id': '1', 'path': 'c/1'}]
    with pytest.raises(ValueError): dual_actions(projection, preview, [{'kind': 'tv', 'tmdb_id': 1, 'season': 2}])


def test_single_quality_targets_only(projection):
    files = [{'id': '1', 'path': 'c/1'}, {'id': '9', 'path': 'c/other'}]
    preview = {'scan_complete': True, 'single': [
        {'kind':'tv', 'tmdb_id':1, 'season':1, 'episode':1, 'side':'cloud', 'reason':'quality', 'winner':1, 'files':files}]}
    selection = {'kind':'tv', 'tmdb_id':1, 'season':1, 'episode':1, 'side':'cloud', 'reason':'quality'}
    assert single_actions(projection, preview, [selection]) == [{'side':'cloud', 'id':'1', 'path':'c/1'}]
    # 缺集季已经统一走双库动作；旧的 episode_gap / covered_episode 请求一律拒绝。
    with pytest.raises(ValueError):
        single_actions(projection, preview, [{**selection, 'reason':'episode_gap'}])
    with pytest.raises(ValueError):
        single_actions(projection, preview, [{**selection, 'reason':'unknown_quality'}])


def test_explicit_special_delete_allows_two_s00_sides_but_not_forged_reasons(projection):
    projection['items'][0]['cloud'].append({'id':'c0', 'path':'c/s00', 'season':0})
    projection['items'][0]['share'].append({'id':'s0', 'path':'s/s00', 'season':0})
    common = {'kind':'tv', 'tmdb_id':1, 'season':0}
    local = {**common, 'winner':'share', 'action_kind':'loc', 'action_reason':'special_delete_local'}
    share = {**common, 'winner':'cloud', 'action_kind':'shr', 'action_reason':'special_delete_share'}
    preview = {'scan_complete':True, 'dual':[local, share]}
    assert {v['id'] for v in dual_actions(projection, preview, [local, share])} == {'c0','s0'}
    forged = {**share, 'action_reason':'special_local_better'}
    with pytest.raises(ValueError, match='同一季'):
        dual_actions(projection, {'scan_complete':True,'dual':[local, forged]}, [local, forged])


def test_one_governance_plan_can_include_more_than_100_works():
    works, verdicts, selections = [], [], []
    for n in range(101):
        works.append({'kind':'movie','tmdb_id':n+1,'cloud':[],
                      'share':[{'id':str(n),'path':'share/%d.strm' % n}]})
        v = {'kind':'movie','tmdb_id':n+1,'season':None,'winner':'cloud','action_kind':'shr'}
        verdicts.append(v); selections.append(v)
    result = dual_actions({'complete':True,'items':works}, {'scan_complete':True,'dual':verdicts}, selections)
    assert len(result) == 101


def test_prepare_cloud_returns_three_values_even_on_error():
    """核查异常时必须返回三元组：之前漏了会造成 'too many values to unpack'。"""
    from strimkeep.deep_delete import _prepare_cloud
    info, status, message = _prepare_cloud(object(), {})
    assert info is None and status == 'failed' and '核查失败' in message


class _One:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return self.value


def test_prepare_cloud_success_returns_info_status_message(tmp_path):
    """成功分支必须返回 (info, None, '')：之前返回字典，解包即崩，导致所有删除都失败。"""
    import hashlib
    import json
    from pathlib import Path
    from strimkeep import deep_delete

    root = Path(tmp_path)
    strm = root / '媒体库/剧 (2020) {tmdb-1}/Season 1/S01E01.1080p.strm'
    strm.parent.mkdir(parents=True)
    content = b'#EXTM3U\n'
    strm.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    relative = strm.relative_to(root).as_posix()
    source = {'id': '11', 'name': 'E01.mkv', 'parent_id': '22', 'size': 123, 'sha1': 'a' * 40}

    class DB:
        def execute(self, sql, args):
            if 'output_registry' in sql:
                # 线上这两列是 JSON 文本
                return _One({'files': json.dumps({relative: digest}),
                             'source': json.dumps(source),
                             'playback_id': '33', 'relative_path': relative})
            if 'owned_outputs' in sql:
                return _One({'account_id': 'acc', 'file_id': '11', 'digest': digest})
            return _One(None)

    class Store:
        def __init__(self):
            self.db = DB()
            self.root = root

        def meta_json(self, key, default=None):
            return None

        def set_meta(self, key, value):
            pass

    class Index:
        def account(self, _):
            return 'acc'

    class Engine:
        def __init__(self):
            self.store = Store()
            self.root = root
            self.index = Index()

    info, status, message = deep_delete._prepare_cloud(
        Engine(), {'file_id': '11', 'playback_id': '33', 'relative_path': relative})
    assert isinstance(info, dict), (status, message)
    assert status is None and message == ''
    assert info['fid'] == '11' and info['pid'] == '22'
