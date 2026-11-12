"""隔离契约（3.0）：目录是事实，单个文件的问题只影响它自己。

历史版本要求「目录与台账完全一致 + 任何无法归属的文件都拒绝生成计划」，
所以这里的用例曾断言 409 / ValueError。3.0 起改成逐文件隔离：
无法归属或来源核不上的文件只进 unsupported 列表，其他作品照常治理。
"""
from fastapi.testclient import TestClient
from test_inventory_operations_api import setup_app, wait
from strimkeep.inventory_partition import partition
from strimkeep.inventory_fs import scan

ROOTS = {'cloud': 'cloud', 'share': '分享影视库'}


def add_unregistered(root, folder, filename='Show.S01E02.1080p.strm'):
    path = root / '分享影视库' / '剧集' / folder / 'Season 01' / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('http://example/unregistered')
    return path


def test_unrelated_unregistered_work_does_not_block_safe_delete(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    unrelated = add_unregistered(tmp_path, 'Other {tmdb-99}')
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        r = client.post('/api/v1/inventory/delete/plan',
                        json={'mode': 'manual', 'selections': [{'kind': 'tv', 'tmdb_id': 12, 'side': 'share'}]})
        assert r.status_code == 200, r.text
        pid = r.json()['plan_id']
        assert client.post(f'/api/v1/inventory/delete/{pid}/execute').status_code == 200
        status = wait(client, pid)
        assert status['state'] == 'completed', status
        assert not path.exists() and unrelated.exists()


def test_same_title_other_tmdb_does_not_block_plan(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    other = add_unregistered(tmp_path, 'Show {tmdb-13}')
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        r = client.post('/api/v1/inventory/delete/plan',
                        json={'mode': 'manual', 'selections': [{'kind': 'tv', 'tmdb_id': 12, 'side': 'share'}]})
        assert r.status_code == 200, r.text
        assert path.exists() and other.exists()


def test_new_related_unregistered_file_after_plan_blocks_execution(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        r = client.post('/api/v1/inventory/delete/plan',
                        json={'mode': 'manual', 'selections': [{'kind': 'tv', 'tmdb_id': 12, 'side': 'share'}]})
        assert r.status_code == 200, r.text
        related = add_unregistered(tmp_path, 'Show {tmdb-12}', 'Show.S01E09.1080p.strm')
        pid = r.json()['plan_id']
        client.post(f'/api/v1/inventory/delete/{pid}/execute')
        status = wait(client, pid)
        assert status['state'] == 'failed' and path.exists() and related.exists()


def test_unrelated_missing_registration_does_not_block_plan(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    other = '分享影视库/剧集/Other {tmdb-99}/Season 01/Other.S01E01.strm'
    db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?,?,?,?)',
               ('s2', 'code', '', 'f2', '', other, 'hash', 0))
    db.commit()
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        r = client.post('/api/v1/inventory/delete/plan',
                        json={'mode': 'manual', 'selections': [{'kind': 'tv', 'tmdb_id': 12, 'side': 'share'}]})
        assert r.status_code == 200, r.text
    assert path.exists()


def test_unattributable_root_file_is_reported_but_allows_other_work(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    stray = tmp_path / '分享影视库' / 'unknown.strm'
    stray.write_text('http://example/unknown')
    scoped = partition(tmp_path, ROOTS, scan(tmp_path, ROOTS, db), 'title_year', db, 'account')
    assert [item['path'] for item in scoped['unsupported']] == ['分享影视库/unknown.strm']
    assert scoped['projection']['items'], '其他作品仍应进入投影'
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        r = client.post('/api/v1/inventory/delete/plan',
                        json={'mode': 'manual', 'selections': [{'kind': 'tv', 'tmdb_id': 12, 'side': 'share'}]})
        assert r.status_code == 200, r.text
    assert path.exists() and stray.exists()


def test_scan_reports_isolation_counts_without_audit_gate(tmp_path, monkeypatch):
    from strimkeep.inventory_api import install_inventory
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    add_unregistered(tmp_path, 'Other {tmdb-99}')          # 分享侧未登记：可正常清理
    stray = tmp_path / 'cloud' / '剧集' / 'stray.strm'      # 认不出作品：进 unsupported
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text('http://example/stray')
    install_inventory(app, app.state.organizer.store, admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        r = client.post('/api/v1/inventory/preview', json={'tie': 'cloud'})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data['scan_complete'] and data['isolated_records'] >= 1
        assert data['audit'] is None
        assert any(item['path'].endswith('stray.strm') for item in data['unsupported'])
        assert 'scan_revision' in data


def test_file_without_tmdb_id_is_reported_as_issue(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    add_unregistered(tmp_path, 'Other')
    scoped = partition(tmp_path, ROOTS, scan(tmp_path, ROOTS, db), 'tmdb', db, 'account')
    assert any(item['reason'] == 'identity_unresolved'
               for item in scoped['projection']['issues']), '缺编号的文件记成 issue'
    assert scoped['projection']['items'], '带编号的作品仍应正常投影'
    assert path.exists()
