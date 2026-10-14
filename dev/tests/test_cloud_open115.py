"""Open authorization uses owned AppID, encrypted rotation and stable account identity."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from app.cloud import accounts, credentials, store, settings, open115, provider115
from app.cloud.errors import CloudError


def grant(cloud, expired=False):
    value = open115.tokens(
        {
            "access_token": "access-one",
            "refresh_token": "refresh-one",
            "expires_in": 7200,
        },
        "123456",
    )
    if expired:
        value["expires_at"] = time.time() - 1
    accounts.save_authorization("123", {"name": "测试账号"}, {"open": value})
    return value


def test_cookie_and_open_authorizations_merge_without_losing_either(cloud):
    grant(cloud)
    accounts.save_verified("UID=123_A1_1; CID=new; SEID=new", cloud.remote)
    _, saved = credentials.load(cloud.id)
    assert (
        saved["open"]["refresh_token"] == "refresh-one" and "CID=new" in saved["cookie"]
    )
    exposed = json.dumps(accounts.describe())
    assert (
        "access-one" not in exposed
        and "refresh-one" not in exposed
        and "CID=" not in exposed
    )
    assert (
        accounts.describe()[0]["authorization"]["cookie"]
        and accounts.describe()[0]["authorization"]["open"]
    )


def test_open_requires_owned_application_id_and_does_not_use_sdk_default(
    cloud, monkeypatch
):
    sdk = Mock()
    monkeypatch.setattr(accounts, "sdk", sdk)
    with pytest.raises(CloudError, match="本应用"):
        accounts.start("owner", mode="open")
    sdk.assert_not_called()


def test_pkce_is_random_bound_to_flow_and_kept_off_client(cloud, monkeypatch):
    settings.save({"open_app_id": "123456"})
    calls = []

    def token(payload, **kwargs):
        calls.append(payload)
        return {
            "code": 0,
            "data": {
                "uid": "flow",
                "time": 1,
                "sign": "s",
                "qrcode": "https://115.com/scan",
            },
        }

    monkeypatch.setattr(
        accounts, "sdk", lambda: SimpleNamespace(login_qrcode_token_open=token)
    )
    first = accounts.start("owner", mode="open", account_id=cloud.id)
    second = accounts.start("owner", mode="open", account_id=cloud.id)
    assert (
        calls[0]["client_id"] == "123456"
        and calls[0]["code_challenge_method"] == "sha256"
    )
    assert calls[0]["code_challenge"] != calls[1]["code_challenge"]
    assert "verifier" not in json.dumps(first) + json.dumps(second)
    assert accounts._FLOWS[first["flow_id"]]["target_uid"] == "123"


def test_rotating_refresh_is_sent_once_across_concurrent_readers(cloud, monkeypatch):
    grant(cloud, True)
    revision = store.account(cloud.id)["revision"]
    refresh = Mock(
        return_value={
            "code": 0,
            "data": {
                "access_token": "access-two",
                "refresh_token": "refresh-two",
                "expires_in": 7200,
            },
        }
    )
    monkeypatch.setattr(
        open115, "sdk", lambda: SimpleNamespace(login_refresh_token_open=refresh)
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: open115.ensure_token(cloud.id), range(2)))
    assert all(value[1]["access_token"] == "access-two" for value in results)
    refresh.assert_called_once()
    assert store.account(cloud.id)["revision"] == revision


def test_refresh_timeout_stays_durable_and_does_not_reuse_old_token(cloud, monkeypatch):
    grant(cloud, True)
    refresh = Mock(side_effect=TimeoutError("token must not leak"))
    monkeypatch.setattr(
        open115, "sdk", lambda: SimpleNamespace(login_refresh_token_open=refresh)
    )
    for _ in range(2):
        with pytest.raises(CloudError):
            open115.ensure_token(cloud.id)
    refresh.assert_called_once()
    assert credentials.load(cloud.id)[1]["open"]["state"] == "needs_reauth"


def test_failure_to_store_new_tokens_does_not_repeat_refresh(cloud, monkeypatch):
    grant(cloud, True)
    original = open115.update
    updates = []

    def update(*args):
        updates.append(1)
        if len(updates) == 2:
            raise CloudError("数据库不可写", 503)
        return original(*args)

    monkeypatch.setattr(open115, "update", update)
    refresh = Mock(
        return_value={
            "state": True,
            "data": {
                "access_token": "access-two",
                "refresh_token": "refresh-two",
                "expires_in": 7200,
            },
        }
    )
    monkeypatch.setattr(
        open115, "sdk", lambda: SimpleNamespace(login_refresh_token_open=refresh)
    )
    with pytest.raises(CloudError):
        open115.ensure_token(cloud.id)
    monkeypatch.setattr(open115, "update", original)
    with pytest.raises(CloudError):
        open115.ensure_token(cloud.id)
    refresh.assert_called_once()


def test_cookie_login_during_refresh_is_preserved(cloud, monkeypatch):
    grant(cloud, True)

    def refresh(*args, **kwargs):
        accounts.save_verified("UID=123_A1_1; CID=new-cookie; SEID=new", cloud.remote)
        return {
            "code": 0,
            "data": {
                "access_token": "access-two",
                "refresh_token": "refresh-two",
                "expires_in": 7200,
            },
        }

    monkeypatch.setattr(
        open115, "sdk", lambda: SimpleNamespace(login_refresh_token_open=refresh)
    )
    open115.ensure_token(cloud.id)
    saved = credentials.load(cloud.id)[1]
    assert (
        "new-cookie" in saved["cookie"]
        and saved["open"]["refresh_token"] == "refresh-two"
    )


def test_new_open_login_is_not_overwritten_by_late_refresh(cloud, monkeypatch):
    grant(cloud, True)

    def refresh(*args, **kwargs):
        value = open115.tokens(
            {
                "access_token": "brand-new-access",
                "refresh_token": "brand-new-refresh",
                "expires_in": 7200,
            },
            "123456",
        )
        accounts.save_authorization("123", {}, {"open": value})
        return {
            "code": 0,
            "data": {
                "access_token": "late-access",
                "refresh_token": "late-refresh",
                "expires_in": 7200,
            },
        }

    monkeypatch.setattr(
        open115, "sdk", lambda: SimpleNamespace(login_refresh_token_open=refresh)
    )
    assert open115.ensure_token(cloud.id)[1]["access_token"] == "brand-new-access"


def test_open_sdk_contract_no_implicit_login_and_exact_delete_id(monkeypatch):
    from p115client import P115OpenClient

    calls = []

    def request(self, *args, **kwargs):
        calls.append(kwargs)
        return (
            {"state": True, "data": {"user_id": "123", "user_name": "测试"}}
            if "/user/info" in kwargs["url"]
            else {"state": True}
        )

    monkeypatch.setattr(P115OpenClient, "request", request)
    provider = open115.OpenProvider({"access_token": "valid-access"}, "123")
    assert provider.client.app_id == 0 and not provider.client.refresh_token
    provider.delete("4")
    assert calls[-1]["url"] == "https://proapi.115.com/open/ufile/delete" and calls[-1][
        "data"
    ] == {"file_ids": "4"}
    with pytest.raises(CloudError):
        provider.move("4", "2")
    assert len(calls) == 2


def test_open_profile_must_match_current_account(monkeypatch):
    fake = SimpleNamespace(
        headers={}, user_info=lambda **kw: {"code": 0, "data": {"user_id": "999"}}
    )
    monkeypatch.setattr(open115, "sdk", lambda: lambda **kw: fake)
    with pytest.raises(CloudError, match="其他账号"):
        open115.OpenProvider({"access_token": "valid"}, "123")


@pytest.mark.parametrize(
    "payload", [{"state": False, "code": 0}, {"code": 5}, {"data": {}}, None]
)
def test_open_error_shapes_are_not_accepted_as_success(payload):
    with pytest.raises(CloudError):
        open115.checked(payload)


def test_naming_presets_keep_type_identity_and_original_extension(cloud):
    from app.cloud import recognition

    row = cloud.remote.info("4")
    settings.save({"naming_style": "compact", "movie_folder": "我的电影"})
    result = recognition.recognize(row["name"])
    assert (
        result["directory"].startswith("我的电影/")
        and "[tmdb-44]" in result["directory"]
    )
    assert "[tmdb-" not in result["name"] and result["name"].endswith(".mkv")
    settings.save({"naming_style": "original"})
    assert recognition.recognize(row["name"])["name"] == row["name"]


@pytest.mark.parametrize(
    "outcome", ["success", "wrong_uid", "exchange_timeout", "cancelled"]
)
def test_open_confirmed_qr_exchanges_once_and_never_saves_wrong_or_cancelled_account(
    cloud, monkeypatch, outcome
):
    settings.save({"open_app_id": "123456"})

    def token(*args, **kwargs):
        return {
            "state": True,
            "data": {
                "uid": "flow",
                "time": 1,
                "sign": "s",
                "qrcode": "https://115.com/scan",
            },
        }

    exchange = Mock(
        return_value={
            "code": 0,
            "data": {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 7200,
            },
        }
    )
    if outcome == "exchange_timeout":
        exchange.side_effect = TimeoutError("secret")
    sdk = SimpleNamespace(
        login_qrcode_token_open=token,
        login_qrcode_scan_status=lambda *a, **kw: {
            "state": True,
            "data": {"status": 2},
        },
        login_qrcode_access_token_open=exchange,
    )
    monkeypatch.setattr(accounts, "sdk", lambda: sdk)
    flow = accounts.start("owner", mode="open", account_id=cloud.id)

    def provider(value, uid):
        if outcome == "wrong_uid":
            raise CloudError("其他账号", 409)
        if outcome == "cancelled":
            accounts.cancel(flow["flow_id"], "owner")
        return SimpleNamespace(
            uid="123", profile=lambda: {"uid": "123", "name": "已授权"}
        )

    monkeypatch.setattr(open115, "OpenProvider", provider)
    if outcome == "success":
        assert accounts.poll(flow["flow_id"], "owner")["phase"] == "connected"
        saved = credentials.load(cloud.id)[1]
        assert saved["open"]["refresh_token"] == "new-refresh" and saved["cookie"]
    else:
        with pytest.raises(CloudError):
            accounts.poll(flow["flow_id"], "owner")
        assert "open" not in credentials.load(cloud.id)[1]
    with pytest.raises(CloudError):
        accounts.poll(flow["flow_id"], "owner")
    exchange.assert_called_once()


@pytest.mark.parametrize(
    "folder", ["..", "bad/name", "bad\\name", "bad:folder", " trailing", "bad*folder"]
)
def test_naming_configuration_rejects_invalid_directory_component(cloud, folder):
    with pytest.raises(CloudError):
        settings.save({"movie_folder": folder})


def test_sdk_invalid_access_never_triggers_implicit_refresh_or_second_delete(
    monkeypatch,
):
    import p115client.client as module
    from p115client import P115OpenClient

    calls = []

    def request(**kwargs):
        calls.append(kwargs["url"])
        return (
            {"state": True, "data": {"user_id": "123"}}
            if kwargs["url"].endswith("/user/info")
            else {"state": False, "errno": 40140125}
        )

    monkeypatch.setattr(
        module,
        "get_request",
        lambda url, **kw: (request, {"url": url, "headers": kw["headers"]}),
    )
    refresh = Mock(side_effect=AssertionError("no implicit refresh"))
    monkeypatch.setattr(P115OpenClient, "refresh_access_token", refresh)
    provider = open115.OpenProvider({"access_token": "valid-access"}, "123")
    with pytest.raises(CloudError):
        provider.delete("4")
    refresh.assert_not_called()
    assert len([x for x in calls if x.endswith("/delete")]) == 1


@pytest.mark.parametrize("response", [None, {}, [], {"data": {}}])
def test_malformed_write_receipt_is_uncertain_and_never_treated_as_rejection(response):
    provider = object.__new__(open115.OpenProvider)
    provider.client = SimpleNamespace(fs_delete=lambda *args, **kw: response)
    with pytest.raises(CloudError) as error:
        provider.delete("4")
    assert error.value.uncertain


def test_owned_app_id_environment_fallback_survives_previous_default_settings(
    cloud, monkeypatch
):
    monkeypatch.setenv("STRIMKEEP_115_APP_ID", "654321")
    assert settings.get()["open_app_id"] == "654321"
    settings.save({"open_app_id": "123456"})
    assert settings.get()["open_app_id"] == "123456"
