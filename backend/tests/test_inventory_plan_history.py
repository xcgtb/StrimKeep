"""计划历史：只取最近 20 条，坏数据不能拖垮接口。

现场事故：库里 52 条计划、合计 32MB。旧实现每次都把**全部**计划的 JSON 解析两遍、
再逐条查两次数据库，打开库存页就把事件循环堵到看门狗打印堆栈。
"""
import json
import time

from fastapi.testclient import TestClient
from test_inventory_operations_api import setup_app


def put(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', (key, value))


def test_plan_history_returns_only_the_newest_twenty(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    now = time.time()
    for index in range(30):
        pid = '%032x' % (index + 1)
        payload = {'state': 'done', 'created': now + index, 'message': '',
                   'snapshot': {'actions': [{'kind': 'tv'}, {'kind': 'tv'}, {'kind': 'tv'}]},
                   'progress': [{'action': 'checked'}]}
        put(db, 'inventory-plan:' + pid, json.dumps(payload))
        put(db, 'inventory-plan-mode:' + pid, json.dumps({'mode': 'governance'}))
    db.commit()

    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        response = client.get('/api/v1/inventory-plans')

    assert response.status_code == 200, response.text
    items = response.json()['items']
    assert len(items) == 20, len(items)
    created = [item['created'] for item in items]
    assert created == sorted(created, reverse=True), '最新的在前'
    assert created[0] == now + 29
    assert {item['mode'] for item in items} == {'governance'}
    assert all(item['count'] == 3 and item['done'] == 1 for item in items)


def test_broken_plan_rows_are_skipped_without_breaking_the_list(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    now = time.time()
    good = '%032x' % 7
    put(db, 'inventory-plan:' + good,
        json.dumps({'state': 'done', 'created': now, 'snapshot': {'actions': []}}))
    put(db, 'inventory-plan:' + '%032x' % 8, '{not json')          # 解析不了
    put(db, 'inventory-plan:' + '%032x' % 9, json.dumps({'state': 'done'}))  # 缺 snapshot
    put(db, 'inventory-plan:legacy-1',                            # 非 32 位编号
        json.dumps({'state': 'done', 'created': now, 'snapshot': {'actions': []}}))
    db.commit()

    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        response = client.get('/api/v1/inventory-plans')

    assert response.status_code == 200, response.text
    plan_ids = [item['plan_id'] for item in response.json()['items']]
    assert good in plan_ids
    assert '%032x' % 8 not in plan_ids and '%032x' % 9 not in plan_ids
    # 非 32 位编号的不是合法计划，和 _load 的口径一致：列表里不出现
    assert 'legacy-1' not in plan_ids


def test_plan_detail_lists_files_and_results(tmp_path, monkeypatch):
    """历史计划点开要能看到：治理条目、文件清单、逐条结果。"""
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    pid = 'a' * 32
    payload = {'state': 'completed', 'created': 100, 'finished': 200, 'message': '',
               'snapshot': {'actions': [{'side': 'share', 'id': '1', 'path': 'x/a.strm'},
                                        {'side': 'cloud', 'id': '2', 'path': 'y/b.strm'}]},
               'progress': [{'side': 'share', 'id': '1', 'path': 'x/a.strm'}]}
    put(db, 'inventory-plan:' + pid, json.dumps(payload))
    put(db, 'inventory-plan-context:' + pid, json.dumps({
        'mode': 'governance',
        'entries': [{
            'title': '走马上任 (2026)', 'season': 1, 'reason_label': '库内多版本-删低版',
            'decisive': '音频规格', 'tie': False, 'ratio': 1.0, 'threshold': 0.9,
            'episodes': {'share': 2, 'cloud': 2}, 'dup_episodes': [28],
            'files': [{'side': 'share', 'id': '1', 'path': 'x/a.strm'}]}],
        'others': [{'side': 'cloud', 'id': '2', 'path': 'y/b.strm'}]}))
    db.commit()

    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        response = client.get('/api/v1/inventory/plans/%s/detail' % pid)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data['count'] == 2 and data['done'] == 1
    assert [file['done'] for file in data['files']] == [True, False]
    assert data['finished'] == 200
    entry = data['entries'][0]
    assert entry['title'] == '走马上任 (2026)' and entry['reason_label'] == '库内多版本-删低版'
    assert entry['decisive'] == '音频规格' and entry['dup_episodes'] == [28]
    assert entry['done'] == 1 and entry['files'][0]['done'] is True
    assert data['others'][0]['done'] is False


def test_expired_plan_is_marked_expired(tmp_path, monkeypatch):
    """超过有效期没执行的计划：列表刷新时标「已过期」，不再一直显示待执行。"""
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    pid = 'b' * 32
    payload = {'state': 'planned', 'created': 100, 'expires': 200, 'message': '',
               'snapshot': {'actions': []}}
    put(db, 'inventory-plan:' + pid, json.dumps(payload))
    db.commit()

    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        response = client.get('/api/v1/inventory-plans')

    assert response.status_code == 200, response.text
    value = json.loads(db.execute('select value from meta where key=?',
                                  ('inventory-plan:' + pid,)).fetchone()[0])
    assert value['state'] == 'expired'
    assert '过期' in value['message']
