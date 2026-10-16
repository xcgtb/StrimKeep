"""Batch scan submissions, retaining per-path identity checks and retry state."""
import json
import time
from unittest.mock import Mock
from app.cloud import emby_library as emby, recognition, settings, store, strm
from app.cloud.errors import CloudError
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_linkage import connection


def assets(cloud, count=3):
    connection(cloud)
    settings.save({"nfo_enabled": False})
    values = []
    for index in range(count):
        media = recognition.recognize("Movie.2024.1080p.mkv")
        media["name"] = "Movie.part"+str(index)+".mkv"
        source = {**cloud.remote.info("4"), "id": str(80+index), "name": media["name"]}
        values.append(strm.generate(cloud.id, source, media))
    return values


def test_three_paths_request_one_refresh_and_confirm_independently(cloud, monkeypatch):
    values = assets(cloud)
    calls = []
    def request(c, path, **kwargs):
        calls.append((path, kwargs))
        return {}
    monkeypatch.setattr(emby, "request", request)
    monkeypatch.setattr(emby, "confirmed_item", lambda c, a, p: {"Id": "confirmed"} if a["id"] == values[0]["id"] else None)
    for _ in values:
        emby.poll_one()
    assert len(calls) == 1 and calls[0][0] == "/Library/Media/Updated"
    updates = calls[0][1]["body"]["Updates"]
    assert len(updates) == 3 and len({u["Path"] for u in updates}) == 3
    records = emby.records(cloud.id)
    assert sum(r["state"] == "confirmed" for r in records) == 1
    assert sum(r["state"] == "pending" for r in records) == 2
    assert all(r["title"] == "Movie" and r["path"].startswith("/strm/") for r in records)


def test_refresh_failure_does_not_mark_batch_as_submitted(cloud, monkeypatch):
    assets(cloud)
    request = Mock(side_effect=CloudError("刷新连接失败", 502))
    monkeypatch.setattr(emby, "request", request)
    emby.poll_one()
    with store.db() as con:
        payloads = [json.loads(r[0]) for r in con.execute("SELECT payload FROM cloud_ingestion")]
    assert all("refresh_attempt" not in p for p in payloads)
    assert all(r["state"] == "pending" for r in emby.records(cloud.id))


def test_batch_does_not_submit_stale_or_revoked_assets(cloud, monkeypatch):
    values = assets(cloud)
    with store.db() as con:
        con.execute("UPDATE cloud_assets SET state='revoked' WHERE id=?", (values[1]["id"],))
    calls = []
    monkeypatch.setattr(emby, "request", lambda c, path, **kw: calls.append(kw) or {})
    monkeypatch.setattr(emby, "confirmed_item", lambda *a: None)
    emby.poll_one()
    assert len(calls[0]["body"]["Updates"]) == 2


def test_timed_retry_resubmits_unconfirmed_paths_without_full_library_scan(cloud, monkeypatch):
    assets(cloud)
    calls = []
    monkeypatch.setattr(emby, "request", lambda c, path, **kw: calls.append((path,kw)) or {})
    monkeypatch.setattr(emby, "confirmed_item", lambda *a: None)
    emby.poll_one()
    with store.db() as con:
        con.execute("UPDATE cloud_ingestion SET attempts=10,next_check=?", (time.time()-1,))
    emby.poll_one()
    assert len(calls) == 2
    assert all(path == "/Library/Media/Updated" and len(kw["body"]["Updates"]) == 3 for path,kw in calls)


def test_worker_advances_a_bounded_batch_without_any_telegram_card(cloud, monkeypatch):
    assets(cloud, count=18)
    settings.save({'notify_enabled': True})
    from app import tg
    sender = Mock(side_effect=AssertionError('Emby receipts must stay web-only'))
    monkeypatch.setattr(tg, 'notify_telegram_card', sender)
    monkeypatch.setattr(tg, 'notify_telegram', sender)
    monkeypatch.setattr(emby, 'request', lambda *args, **kw: {})
    monkeypatch.setattr(emby, 'confirmed_item', lambda *args: {'Id': 'confirmed'})
    # Deterministic clock: exercise the quantity bound independently of timing.
    monkeypatch.setattr(emby.time, 'monotonic', lambda: 1)
    emby.poll_batch()
    records = emby.records(cloud.id)
    assert sum(r['state'] == 'confirmed' for r in records) == 16
    emby.poll_batch()
    assert all(r['state'] == 'confirmed' for r in emby.records(cloud.id))
    sender.assert_not_called()
