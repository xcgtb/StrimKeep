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
