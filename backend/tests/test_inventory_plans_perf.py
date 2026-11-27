"""事件循环阻塞回归：inventory-plan 列表的 JSON SQL 必须在工作线程执行。"""
import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from strimkeep.inventory_operations_api import install_inventory_operations
from strimkeep.schema_migrations import INVENTORY_DISPATCH_DDL


class Store:
    def __init__(self, conn):
        self.db = conn
        self.root = ''


def test_inventory_plans_runs_sql_off_event_loop(monkeypatch):
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)')
    conn.execute(INVENTORY_DISPATCH_DDL)
    for index in range(32):
        conn.execute(
            'INSERT INTO meta VALUES(?,?)',
            ('inventory-plan:%032x' % index,
             json.dumps({'state': 'done', 'created': 1000 + index,
                         'snapshot': {'actions': [{'id': 1}]}, 'progress': []})))
    conn.commit()

    app = FastAPI()
    store = Store(conn)
    calls = []

    async def admin(_request):
        return None

    app.dependency_overrides[admin] = lambda: None
    install_inventory_operations(app, store, admin)
    real_to_thread = asyncio.to_thread

    async def tracked_to_thread(func, *args, **kwargs):
        calls.append((func, args))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr('strimkeep.inventory_operations_api.asyncio.to_thread', tracked_to_thread)
    with TestClient(app) as client:
        response = client.get('/api/v1/inventory-plans')
    assert response.status_code == 200
    assert len(response.json()['items']) == 20
    assert calls, 'inventory-plans 仍未把 SQL 扫描放到工作线程'
