"""End-to-end cloud workflow with a stateful remote fixture and real SQLite/files."""

import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
import pytest
from fastapi.testclient import TestClient
from app import engine, config, main, readiness
from app.cloud import (
    store,
    accounts,
    credentials,
    settings,
    workflow,
    jobs,
    strm,
    playback,
    recognition,
    emby_proxy,
    provider115,
)
from app.cloud.errors import CloudError


class Remote:
    uid = "123"

    def __init__(self):
        self.files = {}
        for fid, parent, name, directory in [
            ("0", "0", "根目录", True),
            ("1", "0", "来源", True),
            ("2", "0", "归档", True),
            ("3", "0", "转存", True),
            ("4", "1", "Movie.2024.1080p.mkv", False),
            ("5", "1", "Movie.2024.1080p.zh.srt", False),
        ]:
            self.files[fid] = {
                "id": fid,
                "parent_id": parent,
                "name": name,
                "is_dir": directory,
                "size": 0 if directory else 100,
                "sha1": "" if directory else "A" * 40,
                "pickcode": "" if directory else "abcdef12345678901",
            }
        self.counter = 20
        self.writes = []
        self.downloads = 0
        self.fail_after_move = False
        self.receive_uncertain = False

    def profile(self):
        return {"uid": self.uid, "name": "测试账号"}

    def list(self, parent):
        return copy.deepcopy(
            [r for k, r in self.files.items() if r["parent_id"] == parent and k != "0"]
        )

    def page(self, parent, offset=0):
        all_rows = self.list(parent)
        return {
            "items": all_rows[offset : offset + 100],
            "total": len(all_rows),
            "offset": offset,
            "has_more": offset + 100 < len(all_rows),
        }

    def info(self, fid):
        if fid not in self.files:
            raise CloudError("文件不存在", 404)
        return copy.deepcopy(self.files[fid])

    def mkdir(self, parent, name):
        self.counter += 1
        fid = str(self.counter)
        self.files[fid] = {
            "id": fid,
            "parent_id": parent,
            "name": name,
            "is_dir": True,
            "size": 0,
            "sha1": "",
            "pickcode": "",
        }
        self.writes.append(("mkdir", fid, parent))
        return fid

    def rename(self, fid, name):
        self.files[fid]["name"] = name
        self.writes.append(("rename", fid, name))

    def move(self, fid, parent):
        self.files[fid]["parent_id"] = parent
        self.writes.append(("move", fid, parent))
        if self.fail_after_move:
            raise CloudError("超时", 503, uncertain=True)

    def share_page(self, code, password, parent="0", offset=0):
        rows = [{**self.info("4"), "id": "999", "parent_id": parent}]
        return {
            "items": rows[offset : offset + 100],
            "total": 1,
            "offset": offset,
            "has_more": False,
        }

    def receive(self, code, password, ids, target):
        self.counter += 1
        fid = str(self.counter)
        self.files[fid] = {**self.info("4"), "id": fid, "parent_id": target}
        self.writes.append(("receive", fid, target))
        if self.receive_uncertain:
            raise CloudError("结果未知", 503, uncertain=True)

    def download(self, fid, pickcode, ua):
        self.downloads += 1
        return {
            "url": "https://download.115cdn.com/movie?t=" + str(int(time.time() + 600)),
            "headers": {"User-Agent": ua},
            "size": 100,
        }


class TMDB:
    key = "configured"

    def get(self, path, **params):
        if path.startswith("/search/"):
            return {
                "results": [{"id": 44, "title": "Movie", "release_date": "2024-01-01"}]
            }
        return {"id": 44, "title": "Movie", "release_date": "2024-01-01"}


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "_operation_pause", lambda: None)
    state = tmp_path / "state"
    state.mkdir()
    output = tmp_path / "media"
    output.mkdir()
    monkeypatch.setattr(engine, "DATA_DIR", tmp_path)
    monkeypatch.setattr(engine, "STATE_DIR", state)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(recognition, "tmdb", lambda: TMDB())
    monkeypatch.setattr(workflow, "refresh_emby", lambda: True)
    monkeypatch.setattr(
        strm, "fetch_sidecar", lambda provider, row: b"subtitle fixture"
    )
    remote = Remote()
    account_id = accounts.save_verified(
        "UID=123_A1_1; CID=private; SEID=private", remote
    )
    settings.save(
        {
            "public_url": "http://nas.example:8321",
            "output_root": str(output),
            "emby_output_root": "/strm",
        }
    )
    monkeypatch.setattr(workflow, "for_account", lambda account_id: remote)
    monkeypatch.setattr(playback, "for_account", lambda account_id: remote)
    monkeypatch.setattr(provider115, "for_account", lambda account_id: remote)
    yield SimpleNamespace(remote=remote, id=account_id, root=output, state=state)
    accounts._FLOWS.clear()
    playback._CACHE.clear()
    emby_proxy._MAP.clear()


def test_actual_sdk_import_and_no_console_login():
    client = provider115.sdk()("UID=123_A1_1; CID=x; SEID=y", console_qrcode=False)
    assert client.user_id == 123


def test_adapter_accepts_flat_and_nested_lists(monkeypatch):
    instance = object.__new__(provider115.Provider115)
    row = {"fid": "4", "cid": "1", "n": "Movie.mkv", "s": 10, "sha1": "abc", "pc": "pc"}
    for envelope in (
        {"state": True, "count": 1, "data": [row]},
        {"state": True, "data": {"count": 1, "data": [row]}},
    ):
        instance.call = lambda *a, **k: envelope
        assert instance.list("1")[0]["id"] == "4"


def test_adapter_does_not_treat_truncation_as_empty():
    instance = object.__new__(provider115.Provider115)
    instance.call = lambda *a, **k: {"state": True, "count": 2, "data": []}
    with pytest.raises(CloudError):
        instance.list("1")


def test_account_credential_encryption_and_identity(cloud):
    row, saved = credentials.load(cloud.id)
    assert row["uid"] == "123" and saved["cookie"].startswith("UID=123")
    assert "private" not in row["sealed"] and "sealed" not in store.accounts()[0]
    revision = row["revision"]
    assert (
        accounts.save_verified("UID=123_A1_1; CID=new; SEID=new", cloud.remote)
        == cloud.id
    )
    assert store.account(cloud.id)["revision"] == revision + 1


def test_missing_key_does_not_replace_it(cloud):
    (cloud.state / ".cloud_credentials_key").unlink()
    with pytest.raises(CloudError):
        credentials.load(cloud.id)
    assert not (cloud.state / ".cloud_credentials_key").exists()


def test_end_to_end_real_sqlite_remote_rename_move_strm_and_play(cloud):
    planned = workflow.plan(cloud.id, "1", "2")
    assert len(planned["items"]) == 1 and len(planned["items"][0]["followers"]) == 1
    jid = workflow.apply(planned["id"], confirmed=True)
    jobs.execute(jid)
    result = store.job(jid)
    assert result["state"] == "success", result
    assert cloud.remote.info("4")["parent_id"] != "1"
    assert cloud.remote.info("4")["name"].startswith("Movie (2024)")
    assert cloud.remote.info("5")["parent_id"] == cloud.remote.info("4")["parent_id"]
    assets = strm.list_assets()
    assert len(assets) == 1
    asset = store.asset(assets[0]["id"])
    path = Path(asset["local_path"])
    assert path.is_file() and path.with_suffix(".nfo").is_file()
    assert "CID=" not in path.read_text() and "/api/cloud/play/" in path.read_text()
    sig = strm.signature(asset["id"], asset["nonce"])
    assert playback.resolve(asset["id"], sig, "Player")["url"].startswith(
        "https://download.115cdn.com/"
    )
    assert playback.resolve(asset["id"], sig, "Player")["url"]
    assert cloud.remote.downloads == 1
    playback.resolve(asset["id"], sig, "Other Player")
    assert cloud.remote.downloads == 2
    with pytest.raises(CloudError):
        workflow.apply(planned["id"], confirmed=True)


def test_move_timeout_verified_without_repeating_write(cloud):
    cloud.remote.fail_after_move = True
    planned = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(planned["id"], True, False)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success"
    assert len([x for x in cloud.remote.writes if x[:2] == ("move", "4")]) == 1


def test_reauthorization_blocks_old_plan(cloud):
    planned = workflow.plan(cloud.id, "1", "2")
    accounts.save_verified("UID=123_A1_1; CID=new; SEID=new", cloud.remote)
    jid = workflow.apply(planned["id"], True, False)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "failed" and not cloud.remote.writes


def test_source_changes_after_preview_preserves_file(cloud):
    planned = workflow.plan(cloud.id, "1", "2")
    cloud.remote.files["4"]["size"] = 200
    jid = workflow.apply(planned["id"], True, False)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "needs_review"
    assert not any(
        x[:2] in (("rename", "4"), ("move", "4")) for x in cloud.remote.writes
    )


def test_transfer_records_target_ids_and_one_shot(cloud):
    preview = workflow.preview_share(cloud.id, "abcdefghi")
    jid = workflow.transfer_submit(preview["preview_id"], ["999"], "3", True)
    jobs.execute(jid)
    result = store.job(jid)
    assert result["state"] == "success", result
    assert result["result"]["files"][0]["id"] != "999"
    with pytest.raises(CloudError):
        workflow.transfer_submit(preview["preview_id"], ["999"], "3", True)
    assert len([x for x in cloud.remote.writes if x[0] == "receive"]) == 1


def test_transfer_timeout_not_automatically_replayed(cloud):
    cloud.remote.receive_uncertain = True
    preview = workflow.preview_share(cloud.id, "abcdefghi")
    jid = workflow.transfer_submit(preview["preview_id"], ["999"], "3", True)
    jobs.execute(jid)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"
    assert len([x for x in cloud.remote.writes if x[0] == "receive"]) == 1


def test_transfer_read_only_reconciliation_records_new_ids(cloud):
    cloud.remote.receive_uncertain = True
    preview = workflow.preview_share(cloud.id, "abcdefghi")
    jid = workflow.transfer_submit(preview["preview_id"], ["999"], "3", True)
    jobs.execute(jid)
    result = jobs.verify_transfer(jid)
    assert (
        store.job(jid)["state"] == "success"
        and result["mapping"][0]["source_id"] == "999"
    )
    assert result["mapping"][0]["target_id"] != "999"
    assert len([x for x in cloud.remote.writes if x[0] == "receive"]) == 1


def test_foreign_strm_never_overwritten(cloud):
    media = recognition.recognize(cloud.remote.info("4")["name"])
    relative = media["directory"] + "/" + Path(media["name"]).with_suffix(".strm").name
    file = cloud.root / relative
    file.parent.mkdir(parents=True)
    file.write_text("external")
    with pytest.raises(CloudError):
        strm.generate(cloud.id, cloud.remote.info("4"), media)
    assert file.read_text() == "external" and strm.list_assets() == []


def test_identical_foreign_metadata_is_not_claimed_or_deleted(cloud):
    row = cloud.remote.info("4")
    media = recognition.recognize(row["name"])
    file = cloud.root / media["directory"] / Path(media["name"]).with_suffix(".nfo")
    file.parent.mkdir(parents=True)
    file.write_bytes(strm.nfo(media))
    asset = strm.generate(cloud.id, row, media, "1")
    assert asset["payload"]["warnings"] and not asset["payload"]["owned"]
    strm.reconcile(cloud.id, "1", set())
    strm.reconcile(cloud.id, "1", set())
    assert file.exists()


def test_same_source_updates_owned_path_with_same_asset_id(cloud):
    row = cloud.remote.info("4")
    media = recognition.recognize(row["name"])
    first = strm.generate(cloud.id, row, media)
    old = Path(first["local_path"])
    changed = {**media, "directory": media["directory"] + " new"}
    second = strm.generate(cloud.id, row, changed)
    assert (
        first["id"] == second["id"]
        and first["nonce"] == second["nonce"]
        and not old.exists()
    )
    assert Path(second["local_path"]).exists()


def test_revoked_asset_denies_network(cloud):
    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]))
    strm.revoke(asset["id"])
    with pytest.raises(CloudError):
        playback.resolve(
            asset["id"], strm.signature(asset["id"], asset["nonce"]), "Player"
        )
    assert cloud.remote.downloads == 0


def test_source_change_denies_playback(cloud):
    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]))
    cloud.remote.files["4"]["sha1"] = "B" * 40
    with pytest.raises(CloudError):
        playback.resolve(
            asset["id"], strm.signature(asset["id"], asset["nonce"]), "Player"
        )
    assert cloud.remote.downloads == 0


def test_complete_scan_prunes_only_its_owned_file_after_two_observations(cloud):
    row = cloud.remote.info("4")
    media = recognition.recognize(row["name"])
    asset = strm.generate(cloud.id, row, media, "1")
    path = Path(asset["local_path"])
    foreign = path.parent / "foreign.strm"
    foreign.write_text("outside ownership")
    first = strm.reconcile(cloud.id, "1", set())
    assert first["deferred"] and path.exists()
    second = strm.reconcile(cloud.id, "1", set())
    assert second["removed"] and not path.exists() and foreign.exists()
    assert cloud.remote.info("4")["id"] == "4"


def test_changed_owned_file_is_never_pruned(cloud):
    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]), "1")
    path = Path(asset["local_path"])
    path.write_text("manually replaced")
    strm.reconcile(cloud.id, "1", set())
    result = strm.reconcile(cloud.id, "1", set())
    assert result["deferred"] and path.read_text() == "manually replaced"


def test_revocation_is_not_undone_by_sync(cloud):
    row = cloud.remote.info("4")
    media = recognition.recognize(row["name"])
    asset = strm.generate(cloud.id, row, media)
    strm.revoke(asset["id"])
    with pytest.raises(CloudError):
        strm.generate(cloud.id, row, media)
    assert store.asset(asset["id"])["state"] == "revoked"


def test_publishing_recovery_uses_exact_file_bytes(cloud):
    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]))
    with store.db() as con:
        con.execute(
            "UPDATE cloud_assets SET state='publishing' WHERE id=?", (asset["id"],)
        )
    strm.recover()
    assert store.asset(asset["id"])["state"] == "active"
    Path(asset["local_path"]).write_text("other data")
    with store.db() as con:
        con.execute(
            "UPDATE cloud_assets SET state='publishing' WHERE id=?", (asset["id"],)
        )
    strm.recover()
    assert store.asset(asset["id"])["state"] == "needs_review"


def test_qr_is_bound_to_browser_session(cloud, monkeypatch):
    fake = SimpleNamespace(
        login_qrcode_token=lambda *a, **k: {
            "state": True,
            "data": {
                "uid": "qruid",
                "time": 1,
                "sign": "qr-sign",
                "qrcode": "https://115.com/scan",
            },
        }
    )
    monkeypatch.setattr(accounts, "sdk", lambda: fake)
    result = accounts.start("browser-A")
    with pytest.raises(CloudError):
        accounts.poll(result["flow_id"], "browser-B")
    assert accounts.image(result["flow_id"], "browser-A").startswith(b"\x89PNG")


def test_no_network_on_invalid_play_signature(cloud):
    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]))
    with pytest.raises(CloudError):
        playback.resolve(asset["id"], "0" * 64, "Player")
    assert cloud.remote.downloads == 0


@pytest.mark.parametrize(
    "url", ["http://nas:bad", "http://bad host", "http://nas/\nother"]
)
def test_invalid_public_address_is_rejected(cloud, url):
    with pytest.raises(CloudError):
        settings.save({"public_url": url})


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/video",
        "https://private.115cdn.com:22/video",
        "https://evil.example/video",
        "https://u:p@cdn.115cdn.com/v",
    ],
)
def test_download_url_origin_checks(url):
    with pytest.raises(CloudError):
        playback.safe_url(url)


def test_emby_proxy_current_request_must_authenticate(cloud, monkeypatch):
    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]))
    settings.save({"proxy_enabled": True})
    monkeypatch.setattr(emby_proxy, "upstream", lambda: "http://emby.local:9000")
    original = httpx.AsyncClient
    received = []

    def handle(request):
        received.append(request)
        return httpx.Response(401, stream=httpx.ByteStream(b"denied"))

    monkeypatch.setattr(
        emby_proxy.httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, transport=httpx.MockTransport(handle), **k),
    )
    with TestClient(emby_proxy.proxy_app) as client:
        response = client.get("/Videos/987/stream?Static=true", follow_redirects=False)
    assert (
        response.status_code == 401
        and "location" not in response.headers
        and len(received) == 1
    )


def test_emby_proxy_exact_media_source_and_user_scope(cloud, monkeypatch):
    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]))
    settings.save({"proxy_enabled": True})
    monkeypatch.setattr(emby_proxy, "upstream", lambda: "http://emby.local:9000")
    original = httpx.AsyncClient

    def handle(request):
        if "PlaybackInfo" in request.url.path:
            data = {
                "MediaSources": [
                    {
                        "Id": "source-A",
                        "Path": "/strm/"
                        + str(Path(asset["local_path"]).relative_to(cloud.root)),
                    }
                ]
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=httpx.ByteStream(json.dumps(data).encode()),
            )
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 0-3/4"},
            stream=httpx.ByteStream(b"abcd"),
        )

    monkeypatch.setattr(
        emby_proxy.httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, transport=httpx.MockTransport(handle), **k),
    )
    with TestClient(emby_proxy.proxy_app) as client:
        headers = {"X-Emby-Token": "user-A"}
        assert (
            client.post("/Items/987/PlaybackInfo", headers=headers).status_code == 200
        )
        assert (
            client.get(
                "/Videos/987/stream?Static=true&MediaSourceId=source-A",
                headers=headers,
                follow_redirects=False,
            ).status_code
            == 302
        )
        assert (
            client.get(
                "/Videos/987/stream?Static=true&MediaSourceId=source-B",
                headers=headers,
                follow_redirects=False,
            ).status_code
            == 206
        )
        assert (
            client.get(
                "/Videos/987/stream?Static=true&MediaSourceId=source-A",
                headers={"X-Emby-Token": "user-B"},
                follow_redirects=False,
            ).status_code
            == 206
        )
        assert (
            client.get(
                "/Videos/987/stream?MediaSourceId=source-A",
                headers=headers,
                follow_redirects=False,
            ).status_code
            == 206
        )


def test_management_uses_existing_authentication():
    with TestClient(main.app, raise_server_exceptions=False) as client:
        assert client.get("/api/cloud/status").status_code == 401
        assert client.post("/api/cloud/qr", json={}).status_code == 401


def test_qr_confirmation_saves_profile_without_exposing_cookie(cloud, monkeypatch):
    fake = SimpleNamespace(
        login_qrcode_token=lambda *a, **k: {
            "state": True,
            "data": {
                "uid": "qruid",
                "time": 1,
                "sign": "qrsign",
                "qrcode": "https://115.com/scan",
            },
        },
        login_qrcode_scan_status=lambda *a, **k: {"state": True, "data": {"status": 2}},
        login_qrcode_scan_result=lambda *a, **k: {
            "state": True,
            "data": {
                "cookie": {"UID": "123_A1_1", "CID": "private", "SEID": "private"}
            },
        },
    )
    monkeypatch.setattr(accounts, "sdk", lambda: fake)
    monkeypatch.setattr(accounts, "Provider115", lambda *a, **k: cloud.remote)
    flow = accounts.start("browser")
    result = accounts.poll(flow["flow_id"], "browser")
    assert result["phase"] == "connected" and result["account_id"] == cloud.id
    assert store.account(cloud.id)["name"] == "测试账号"
    assert "private" not in json.dumps(result) + json.dumps(store.accounts())
    with pytest.raises(CloudError):
        accounts.poll(flow["flow_id"], "browser")


def test_cancelled_qr_cannot_save_late_confirmation(cloud, monkeypatch):
    token = {
        "state": True,
        "data": {"uid": "qr", "time": 1, "sign": "s", "qrcode": "https://115.com/scan"},
    }
    monkeypatch.setattr(
        accounts,
        "sdk",
        lambda: SimpleNamespace(login_qrcode_token=lambda *a, **k: token),
    )
    flow = accounts.start("owner")
    revision = store.account(cloud.id)["revision"]

    def finish(*a, **k):
        accounts.cancel(flow["flow_id"], "owner")
        return {"state": True, "data": {"cookie": "UID=123_A1_1; CID=late; SEID=late"}}

    monkeypatch.setattr(
        accounts,
        "sdk",
        lambda: SimpleNamespace(
            login_qrcode_scan_status=lambda *a, **k: {
                "state": True,
                "data": {"status": 2},
            },
            login_qrcode_scan_result=finish,
        ),
    )
    with pytest.raises(CloudError):
        accounts.poll(flow["flow_id"], "owner")
    assert store.account(cloud.id)["revision"] == revision


def test_no_silent_key_replacement_on_reauthorization_or_playback(cloud):
    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]))
    (cloud.state / ".cloud_playback_key").unlink()
    with pytest.raises(CloudError):
        strm.signature(asset["id"], asset["nonce"])
    assert not (cloud.state / ".cloud_playback_key").exists()
    (cloud.state / ".cloud_credentials_key").unlink()
    with pytest.raises(CloudError):
        accounts.save_verified("UID=123_A1_1; CID=new; SEID=new", cloud.remote)
    assert not (cloud.state / ".cloud_credentials_key").exists()


def test_sdk_write_payload_contract_preserves_both_files(monkeypatch):
    provider = provider115.Provider115({"cookie": "UID=123_A1_1; CID=x; SEID=y"})
    calls = []

    def request(self, *args, **kwargs):
        calls.append(kwargs)
        return {"state": True}

    monkeypatch.setattr(type(provider.client), "request", request)
    provider.move("4", "2")
    provider.rename("4", "New.mkv")
    provider.receive("abcdefghi", "pass", ["999"], "3")
    assert json.loads(calls[0]["data"]["conflict_policy"]) == {
        "4": {"action": "keep_both"}
    }
    assert calls[0]["data"]["pid"] == "2" and calls[0]["data"]["fid[0]"] == "4"
    assert "New.mkv" in str(calls[1]["data"])
    assert calls[2]["data"]["file_id"] == "999" and calls[2]["data"]["cid"] == "3"


@pytest.mark.parametrize(
    "raw",
    [
        {"fid": "4", "cid": "1", "n": "Movie.mkv", "s": 10, "sha": "abc"},
        {
            "file_id": "4",
            "category_id": "1",
            "file_name": "Movie.mkv",
            "file_size": 10,
            "file_sha1": "abc",
        },
        {"fid": "4", "pid": "1", "fn": "Movie.mkv", "fs": 10, "fc": "1", "sha1": "abc"},
    ],
)
def test_sdk_response_variants_keep_the_same_identity(raw):
    row = provider115.normalize(raw)
    assert (row["id"], row["parent_id"], row["is_dir"], row["size"], row["sha1"]) == (
        "4",
        "1",
        False,
        10,
        "ABC",
    )


def test_partial_transfer_does_not_continue_organization(cloud):
    original = cloud.remote.receive

    def wrong_receipt(*args):
        original(*args)
        fid = cloud.remote.writes[-1][1]
        cloud.remote.files[fid]["size"] = 999

    cloud.remote.receive = wrong_receipt
    preview = workflow.preview_share(cloud.id, "abcdefghi")
    jid = workflow.transfer_submit(preview["preview_id"], ["999"], "3", True, "2")
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"
    assert not any(x[0] in ("rename", "move") for x in cloud.remote.writes)


def test_concurrent_plan_submission_is_one_shot(cloud):
    from concurrent.futures import ThreadPoolExecutor

    planned = workflow.plan(cloud.id, "1", "2")

    def submit():
        try:
            return workflow.apply(planned["id"], True, False)
        except CloudError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sum(bool(r) for r in results) == 1 and len(store.jobs()) == 1


def test_restart_does_not_replay_inflight_writes(cloud, monkeypatch):
    jid = jobs.enqueue("transfer", {"account_id": cloud.id})
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='running' WHERE id=?", (jid,))

    class Thread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return False

        def join(self, *args):
            pass

    monkeypatch.setattr(jobs.threading, "Thread", Thread)
    jobs.start()
    jobs.stop()
    assert store.job(jid)["state"] == "needs_review" and not cloud.remote.writes


def test_scheduler_deduplicates_active_rule_jobs(cloud):
    payload = {
        "account_id": cloud.id,
        "account_revision": 1,
        "source_cid": "1",
        "target_cid": "2",
        "interval_seconds": 300,
    }
    with store.db() as con:
        con.execute(
            "INSERT INTO cloud_rules VALUES(?,?,?,?,?)",
            ("rule", store.encode(payload), 1, 0, 0),
        )
    jobs.schedule()
    with store.db() as con:
        con.execute("UPDATE cloud_rules SET next_run=0")
    jobs.schedule()
    assert len(store.jobs()) == 1


def test_existing_session_api_runs_transfer_organization_and_strm(cloud, monkeypatch):
    from app.routers import deps

    monkeypatch.setattr(readiness, "failed", lambda: False)
    client = TestClient(main.app)
    client.cookies.set(
        deps.SESSION_COOKIE, deps._sign_session(deps.WEB_USER, int(time.time() + 3600))
    )
    headers = {"Origin": "http://testserver"}
    bad = client.post(
        "/api/cloud/plans", json={}, headers={"Origin": "https://elsewhere.example"}
    )
    assert bad.status_code == 403
    preview = client.post(
        "/api/cloud/shares/preview",
        headers=headers,
        json={"account_id": cloud.id, "link": "abcdefghi"},
    )
    assert preview.status_code == 200, preview.text
    response = client.post(
        "/api/cloud/transfers",
        headers=headers,
        json={
            "preview_id": preview.json()["preview_id"],
            "file_ids": ["999"],
            "target_cid": "3",
            "organize_target": "2",
            "generate": True,
            "confirmed": True,
        },
    )
    assert response.status_code == 202, response.text
    jobs.execute(response.json()["job_id"])
    details = client.get("/api/cloud/jobs/" + response.json()["job_id"]).json()["job"]
    assert details["state"] == "success", details
    assets = client.get("/api/cloud/assets").json()["items"]
    assert len(assets) == 1
    content = Path(assets[0]["path"]).read_text()
    assert "/api/cloud/play/" in content and "UID=" not in content
    client.close()


def test_refresh_failure_is_visible_and_notification_is_opt_in(cloud, monkeypatch):
    from app import tg

    sender = Mock(return_value=True)
    monkeypatch.setattr(tg, "notify_telegram", sender)
    monkeypatch.setattr(workflow, "refresh_emby", lambda: False)
    planned = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(planned["id"], True)
    jobs.execute(jid)
    assert (
        store.job(jid)["state"] == "needs_review"
        and store.job(jid)["result"]["refresh_pending"]
    )
    sender.assert_not_called()
    settings.save({"notify_enabled": True})
    jobs.notify(jid, "organize", "needs_review", "Emby 需要刷新")
    sender.assert_called_once()


def test_cookie_required_playback_preserves_range_without_leaking_headers(
    cloud, monkeypatch
):
    import socket

    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]))
    original_download = cloud.remote.download

    def download(*args):
        value = original_download(*args)
        value["headers"]["Cookie"] = "temporary-secret"
        return value

    cloud.remote.download = download
    monkeypatch.setattr(
        playback.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    original = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            206,
            headers={
                "Content-Range": "bytes 0-3/100",
                "Set-Cookie": "never-expose",
                "Content-Length": "4",
            },
            stream=httpx.ByteStream(b"abcd"),
        )

    monkeypatch.setattr(
        playback.httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, transport=httpx.MockTransport(handle), **k),
    )
    with TestClient(main.app) as client:
        sig = strm.signature(asset["id"], asset["nonce"])
        response = client.get(
            "/api/cloud/play/" + asset["id"] + "?sig=" + sig,
            headers={"Range": "bytes=0-3"},
        )
    assert (
        response.status_code == 206
        and response.content == b"abcd"
        and "set-cookie" not in response.headers
    )
    assert (
        requests[0].headers["range"] == "bytes=0-3"
        and requests[0].headers["cookie"] == "temporary-secret"
    )


def test_emby_websocket_relay_preserves_token_and_custom_upstream_port(
    cloud, monkeypatch
):
    import asyncio
    from contextlib import asynccontextmanager
    from websockets.asyncio import client as ws_client

    settings.save({"proxy_enabled": True})
    monkeypatch.setattr(emby_proxy, "upstream", lambda: "http://emby.local:9000/emby")
    calls = []

    class RemoteSocket:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def send(self, value):
            await self.queue.put("echo:" + value)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.queue.get()

    @asynccontextmanager
    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        yield RemoteSocket()

    monkeypatch.setattr(ws_client, "connect", connect)
    with TestClient(emby_proxy.proxy_app) as client:
        with client.websocket_connect(
            "/socket?api_key=user-token", headers={"X-Emby-Token": "user-token"}
        ) as socket:
            socket.send_text("hello")
            assert socket.receive_text() == "echo:hello"
    assert calls[0][0] == "ws://emby.local:9000/emby/socket?api_key=user-token"
    assert calls[0][1]["additional_headers"]["x-emby-token"] == "user-token"


def test_manual_season_episode_override_names_tv_files(cloud, monkeypatch):
    class TV:
        key = "configured"

        def get(self, path, **params):
            if "/episode/" in path:
                return {
                    "id": 5507,
                    "name": "第七集",
                    "season_number": 0,
                    "episode_number": 7,
                }
            return {"id": 55, "name": "剧集", "first_air_date": "2024-01-01"}

    monkeypatch.setattr(recognition, "tmdb", lambda: TV())
    media = recognition.recognize(
        "episode07.mkv", {"kind": "tv", "tmdb_id": "55", "season": 0, "episode": 7}
    )
    assert media["name"] == "剧集 (2024) - S00E07.mkv"
    assert media["directory"].endswith("Season 00")
    from xml.etree import ElementTree as ET

    nfo = ET.fromstring(strm.nfo(media))
    assert nfo.findtext("uniqueid") == "5507" and nfo.findtext("showtitle") == "剧集"


def test_generated_names_remain_readable_by_existing_governance(cloud):
    media = recognition.recognize(cloud.remote.info("4")["name"])
    assert engine._RE_DIR_TMDB.search(media["directory"]).group(1) == media["tmdb_id"]


def test_sync_keeps_confirmed_media_identity_without_rematching_title(
    cloud, monkeypatch
):
    planned = workflow.plan(cloud.id, "1", "2")
    jobs.execute(workflow.apply(planned["id"], True))

    class BoundTMDB(TMDB):
        def get(self, path, **params):
            assert not path.startswith(
                "/search/"
            ), "confirmed identity must not be matched by title again"
            return super().get(path, **params)

    monkeypatch.setattr(recognition, "tmdb", lambda: BoundTMDB())
    account = store.account(cloud.id)
    result = workflow.sync(
        "sync-id",
        {
            "account_id": cloud.id,
            "account_revision": account["revision"],
            "source_cid": "2",
        },
    )
    assert (
        result["generated"] and not result["skipped"] and len(strm.list_assets()) == 1
    )


def test_sync_does_not_rebind_same_id_to_changed_content(cloud):
    row = cloud.remote.info("4")
    asset = strm.generate(cloud.id, row, recognition.recognize(row["name"]))
    cloud.remote.files["4"]["sha1"] = "B" * 40
    with pytest.raises(CloudError):
        strm.generate(
            cloud.id, cloud.remote.info("4"), recognition.recognize(row["name"])
        )
    assert store.asset(asset["id"])["payload"]["source"]["sha1"] == "A" * 40
