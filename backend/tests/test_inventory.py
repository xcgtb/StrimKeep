from datetime import date

import pytest

from strimkeep.inventory import missing_season, project, work_complete


def add(root, path):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('example', encoding='utf-8')


def test_inventory_groups_by_tmdb_and_side_and_expands_ranges(tmp_path):
    a = 'cloud/Series (2020) {tmdb-12}/Season 01/S01E01-E02.1080p.strm'
    b = 'share/Series (2020) {tmdb-12}/Season 01/S01E01.2160p.strm'
    add(tmp_path, a)
    add(tmp_path, b)
    result = project([('cloud', '1', a), ('share', '2', b)], tmp_path)
    assert result['complete'] and len(result['items']) == 1
    item = result['items'][0]
    assert item['kind'] == 'tv' and item['tmdb_id'] == 12
    assert item['cloud'][0]['episodes'] == [1, 2]
    assert item['share'][0]['episodes'] == [1]


def test_missing_uses_only_aired_official_episodes():
    item = {'cloud': [{'season': 1, 'episodes': [1, 3]}], 'share': [{'season': 1, 'episodes': [1]}]}
    episodes = [{'episode_number': 1, 'air_date': '2020-01-01'},
                {'episode_number': 2, 'air_date': '2020-01-08'},
                {'episode_number': 3, 'air_date': '2030-01-01'},
                {'episode_number': 4, 'air_date': None}]
    result = missing_season(episodes, item, 1, date(2025, 1, 1))
    assert result['cloud']['missing'] == [2]
    assert result['share']['missing'] == [2]
    assert result['unknown_air_date'] == [4]


def test_unreliable_records_are_reported_instead_of_counted(tmp_path):
    unknown = 'X {tmdb-1}/Season 01/no-episode.strm'
    two_ids = 'Y {tmdb-3} {tmdb-4}/Y 2020.strm'
    for name in (unknown, two_ids):
        add(tmp_path, name)
    result = project([('share', '2', unknown), ('cloud', '1', two_ids),
                      ('cloud', '3', '../outside.strm'), ('cloud', '4', 'missing.strm')], tmp_path)
    # 个别文件解析不了只记问题，不再把整库判成「不可用」（0.9.148 台账解耦）。
    assert result['complete'] and result['issue_count'] == 3
    assert {x['reason'] for x in result['issues']} == {'episode_unresolved', 'unsafe_path', 'missing_or_unsafe'}
    assert next(x for x in result['issues'] if x['id'] == '4')['path'] == 'missing.strm'
    # 目录里出现两个 TMDB 时仍是一份作品（1.2.12 把 TMDB 当证据而不是身份），两个编号都记录
    row = next(item for item in result['items'] if item['title'].startswith('Y'))
    assert row['tmdb_ids'] == [3, 4] and row['tmdb_id'] == 3


def test_absent_root_never_looks_like_empty_inventory(tmp_path):
    with pytest.raises(ValueError):
        project([], tmp_path / 'missing')


def test_work_completeness_only_ignores_evidence_of_other_works():
    def snapshot(*issues):
        item = {'kind': 'tv', 'tmdb_id': 12, 'tmdb_ids': [12],
                'cloud': [{'path': 'cloud/Series {tmdb-12}/Season 1/S01E01.strm'}], 'share': []}
        return {'items': [item], 'issues': list(issues)}

    assert work_complete(snapshot(), 12)
    # 别的作品目录里的问题、以及根本认不出作品的散文件，都与这部作品无关。
    assert work_complete(snapshot({'path': 'cloud/other {tmdb-99}/missing.strm'}), 12)
    assert work_complete(snapshot({'path': '分享STRM未识别/foo.strm'}), 12)
    # 自己目录里的文件解析不了，才是「待核实」。
    assert not work_complete(snapshot({'path': 'cloud/Series {tmdb-12}/Season 1/bad.strm'}), 12)
    assert not work_complete(snapshot({'path': 'cloud/{tmdb-12}/{tmdb-100}.strm'}), 12)
    assert not work_complete(snapshot(), 99)


def test_http_inventory_and_missing_season(tmp_path):
    import sqlite3
    from types import SimpleNamespace
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from strimkeep.inventory_api import install_inventory

    relative = 'cloud/Series {tmdb-12}/Season 01/S01E01.1080p.strm'
    add(tmp_path, relative)
    relative2 = 'cloud/Series {tmdb-12}/Season 02/S02E01.1080p.strm'
    add(tmp_path, relative2)
    extra = 'cloud/Series {tmdb-12}/Season 01/S01E04.1080p.strm'
    future = 'cloud/Series {tmdb-12}/Season 01/S01E03.1080p.strm'
    add(tmp_path, extra); add(tmp_path, future)
    (tmp_path / '分享影视库').mkdir()
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE organized_files(file_id TEXT,relative_path TEXT,playback_id TEXT);'
                     'CREATE TABLE share_strms(id TEXT,strm_path TEXT,revoked INTEGER);'
                     'CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);'
                     'CREATE TABLE playbacks(id TEXT,file_id TEXT,revoked INTEGER);'
                     'CREATE TABLE output_registry(file_id TEXT);'
                     'CREATE TABLE owned_outputs(file_id TEXT,relative_path TEXT);'
                     'CREATE TABLE organize_jobs(state TEXT);')
    db.execute('INSERT INTO organized_files(file_id,relative_path) VALUES (?,?)', ('1', relative))
    db.execute('INSERT INTO organized_files(file_id,relative_path) VALUES (?,?)', ('other', relative2))
    db.execute('INSERT INTO organized_files(file_id,relative_path) VALUES (?,?)', ('extra', extra))
    db.execute('INSERT INTO organized_files(file_id,relative_path) VALUES (?,?)', ('future', future))
    db.commit()
    class Organizer:
        emby = SimpleNamespace(connection=lambda: ('http://emby/emby', 'test-key'))
        malformed = False
        async def tmdb(self, path):
            if path == 'tv/12':
                return {'seasons': [{'season_number': 0}, {'season_number': 1}, {'season_number': 2}]}
            if path == 'tv/12/season/2':
                raise RuntimeError('temporary network failure')
            assert path == 'tv/12/season/1'
            if self.malformed:
                return {'episodes': [{'episode_number': 1, 'air_date': '2020-01-01'},
                                     {'episode_number': 1, 'air_date': '2020-01-08'}]}
            return {'episodes': [{'episode_number': 1, 'air_date': '2020-01-01'},
                                 {'episode_number': 2, 'air_date': '2020-01-08'},
                                 {'episode_number': 3, 'air_date': '2099-01-01'}]}
    app = FastAPI()
    app.state.strms = tmp_path
    app.state.organizer = Organizer()
    class Http:
        payload = {'TotalRecordCount': 2, 'Items': [{'Id':'a123', 'Name':'Show', 'ProviderIds':{'Tmdb':'12'}}, {'Id':'b456', 'ProviderIds':{'Tmdb':'13'}}]}
        async def get(self, url, **kwargs):
            data = self.payload
            class Response:
                def raise_for_status(self): pass
                def json(self):
                    return data
            return Response()
    app.state.http = Http()
    import asyncio
    app.state.organizer.publication_gate = asyncio.Lock()
    def admin():
        raise HTTPException(401)
    def set_meta(key, value):
        with db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, 'true' if value else 'false'))
    settings = {'organize_config': {'library_dir_name': 'cloud'}, 'emby_url': 'http://emby:8096'}
    install_inventory(app, SimpleNamespace(db=db, get=settings.get, set_meta=set_meta, set_many=settings.update), admin)
    with TestClient(app) as client:
        assert client.get('/api/v1/inventory').status_code == 401
        app.dependency_overrides[admin] = lambda: None
        response = client.get('/api/v1/inventory')
        assert response.status_code == 200 and response.json()['items'][0]['tmdb_id'] == 12
        assert client.post('/api/v1/inventory/audit').json()['clean']
        add(tmp_path, 'cloud/unregistered.strm')
        assert client.post('/api/v1/inventory/audit').json()['libraries']['cloud']['unregistered_count'] == 1
        response = client.get('/api/v1/inventory/tv/12/season/1')
        assert response.status_code == 200 and response.json()['cloud']['missing'] == [2]
        assert response.json()['share']['missing'] == [1, 2]
        summary = client.get('/api/v1/inventory/tv/12/missing').json()
        assert summary['failed_seasons'] == [2] and not summary['tmdb_complete']
        assert summary['cloud_missing'] == 1 and summary['share_missing'] == 2
        assert summary['combined_missing'] == 1
        assert summary['combined_extra'] == 1
        assert next(s for s in summary['seasons'] if s['season'] == 1)['cloud']['extra'] == [4]
        season2 = next(s for s in summary['seasons'] if s['season'] == 2)
        assert not season2['official_known'] and season2['cloud']['present'] == [1]
        app.state.organizer.malformed = True
        malformed = client.get('/api/v1/inventory/tv/12/missing').json()
        assert not malformed['tmdb_complete'] and 1 in malformed['failed_seasons']
        assert not next(s for s in malformed['seasons'] if s['season'] == 1)['official_known']
        assert client.get('/api/v1/inventory/tv/12/season/1').status_code == 502
        app.state.organizer.malformed = False
        emby = client.get('/api/v1/inventory/tv/12/emby').json()
        assert emby['items'][0]['url'] == 'http://emby:8096/web/index.html#!/item?id=a123'
        app.state.http.payload = {'TotalRecordCount': 10, 'Items': []}
        assert client.get('/api/v1/inventory/tv/12/emby').status_code == 502
        settings['emby_url'] = 'http://user:pass@emby:8096'
        assert client.get('/api/v1/inventory/tv/12/emby').status_code == 409
        result = client.post('/api/v1/inventory/preview', json={'tie': 'share'})
        payload = result.json()
        assert result.status_code == 200 and payload['executable'] is False
        # 磁盘上多出一个没登记的文件不再拦整页：它作为「进不了计划」的文件出现。
        assert not any(row.get('reason') in ('audit_mismatch', 'inventory_incomplete')
                       for row in payload['blocked'])
        assert any(item['path'] == 'cloud/unregistered.strm' for item in payload['unsupported'])
        assert client.post('/api/v1/inventory/preview', json={'tie': 'invalid'}).status_code == 422
        assert client.get('/api/v1/inventory/config').json()['tie'] == 'cloud'
        assert client.put('/api/v1/inventory/config', json={'tie': 'share'}).status_code == 200
        assert client.get('/api/v1/inventory/config').json()['tie'] == 'share'
        assert client.put('/api/v1/inventory/tv/12/manual-complete', json={'complete': True}).json()['manual_complete']
        assert client.get('/api/v1/inventory').json()['items'][0]['manual_complete']
        assert not client.put('/api/v1/inventory/tv/12/manual-complete', json={'complete': False}).json()['manual_complete']
        assert client.put('/api/v1/inventory/tv/13/manual-complete', json={'complete': True}).status_code == 404
        db.execute('INSERT INTO organized_files VALUES (?,?,?)', ('2', 'cloud/missing.strm', 'p2'))
        db.execute('INSERT INTO playbacks VALUES (?,?,?)', ('p2', '2', 1))
        db.commit()
        stale = client.get('/api/v1/inventory/stale-records').json()['items']
        assert len(stale) == 1 and stale[0]['file_id'] == '2'
        fixed = client.post('/api/v1/inventory/stale-records/clear', json={'file_ids':['2']})
        assert fixed.status_code == 200 and fixed.json()['removed'] == 1
        assert db.execute('SELECT count(*) FROM meta WHERE key LIKE ?', ('inventory-stale-backup:%',)).fetchone()[0] == 1
    db.close()


def test_official_episodes_reject_incomplete_or_ambiguous_rows():
    from strimkeep.inventory_api import valid_official_episodes
    assert valid_official_episodes([{'episode_number': 1}, {'episode_number': 2}])
    for rows in ([], None, [{'episode_number': 1}, {'episode_number': 1}],
                 [{'episode_number': None}], [{'episode_number': 0}],
                 [{'episode_number': True}], [{'episode_number': '2'}], [None]):
        assert not valid_official_episodes(rows)


def test_same_tmdb_different_titles_are_diagnostic_not_blocking(tmp_path):
    from strimkeep.inventory_preview import governance_preview
    from strimkeep.inventory_operations import manual_actions
    from strimkeep.quality import public_policy
    a = 'cloud/Alpha (2020) {tmdb-12}/Season 1/S01E01.1080p.strm'
    b = 'share/Beta (2020) {tmdb-12}/Season 1/S01E01.2160p.strm'
    add(tmp_path,a); add(tmp_path,b)
    data = project([('cloud','1',a),('share','2',b)],tmp_path)
    assert data['complete'] and len(data['items']) == 2
    assert data['tmdb_conflicts'][0]['tmdb'] == 12 and data['tmdb_conflicts'][0]['count'] == 2
    assert not work_complete(data,12)
    result = governance_preview(data,public_policy())
    assert not result['dual'] and not result['single'] and not result['blocked']
    with pytest.raises(ValueError,match='多个作品目录'):
        manual_actions(data,[{'kind':'tv','tmdb_id':12,'side':'both'}])


def test_title_identity_matches_legacy_punctuation_normalization(tmp_path):
    a = 'cloud/Alpha.Beta (2020) {tmdb-12}/Season 1/S01E01.1080p.strm'
    b = 'share/Alpha Beta (2020) {tmdb-12}/Season 1/S01E01.2160p.strm'
    add(tmp_path,a); add(tmp_path,b)
    data = project([('cloud','1',a),('share','2',b)],tmp_path)
    assert not data['items'][0].get('identity_conflicts') and work_complete(data,12)


def test_same_title_and_year_with_different_tmdb_is_one_work(tmp_path):
    """1.2.12 `governance_title_key`：身份是剧名+年份，两边写成不同 TMDB 也算同一部。"""
    a = 'cloud/Alpha (2020) {tmdb-12}/Season 1/S01E01.1080p.strm'
    b = 'share/Alpha (2020) {tmdb-99}/Season 1/S01E01.2160p.strm'
    add(tmp_path, a)
    add(tmp_path, b)
    data = project([('cloud', '1', a), ('share', '2', b)], tmp_path)
    assert data['complete'] and len(data['items']) == 1
    item = data['items'][0]
    assert item['tmdb_ids'] == [12, 99] and item['tmdb_id'] == 12
    assert len(item['cloud']) == 1 and len(item['share']) == 1

    from strimkeep.inventory_preview import governance_preview
    from strimkeep.quality import public_policy
    policy = public_policy()
    for rule in policy['rules']:
        rule['enabled'] = rule['key'] == 'resolution'
    row = governance_preview(data, policy)['dual'][0]
    assert (row['action_kind'], row['reason']) == ('loc', 'share_wins')
    assert row['match_basis'] == '剧名 + 年份'


def test_match_strategy_tmdb_pairs_by_id_and_falls_back_on_conflict(tmp_path):
    """身份口径可切换：tmdb 优先；同一编号被两个剧名使用时回退到剧名+年份。"""
    a = 'cloud/Alpha (2020) {tmdb-12}/Season 1/S01E01.1080p.strm'
    b = 'share/Alpha (2020) {tmdb-12}/Season 1/S01E01.2160p.strm'
    c = 'share/Beta (2020) {tmdb-12}/Season 1/S01E01.2160p.strm'
    d = 'cloud/Alpha (2020) {tmdb-99}/Season 1/S01E02.1080p.strm'
    for name in (a, b, c, d):
        add(tmp_path, name)
    rows = [('cloud', '1', a), ('share', '2', b), ('share', '3', c), ('cloud', '4', d)]

    by_title = project(rows, tmp_path, 'title_year')
    assert len(by_title['items']) == 2          # Alpha（12+99 合并）/ Beta
    alpha = next(item for item in by_title['items'] if item['title'].startswith('Alpha'))
    assert alpha['tmdb_ids'] == [12, 99]
    assert alpha['identity_source'] == 'title_year'

    by_tmdb = project(rows, tmp_path, 'tmdb')
    # 12 被 Alpha 与 Beta 同时使用 → 冲突，两个身份都回退；99 只有 Alpha → 走 TMDB
    items = {(item['title'], tuple(item['tmdb_ids'])): item for item in by_tmdb['items']}
    assert len(items) == 3
    assert any(item['identity_source'] == 'tmdb' for item in items.values())
    assert all(item['fallback'] for item in items.values() if 12 in item['tmdb_ids'])
    assert by_tmdb['tmdb_conflicts'][0]['tmdb'] == 12
    assert by_tmdb['match_strategy'] == 'tmdb'
