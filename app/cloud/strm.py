"""Owned STRM references with durable publication intent and source-bound assets."""

import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import threading
import time
from urllib.parse import urlencode
from xml.etree import ElementTree as ET
from app.cloud import credentials, settings, store
from app.cloud.errors import CloudError

_LOCK = threading.RLock()


def signature(asset_id, nonce):
    # Existing references must not silently acquire a new signing key after data loss.
    with store.db() as con:
        required = (
            con.execute("SELECT 1 FROM cloud_assets LIMIT 1").fetchone() is not None
        )
    secret = credentials.key(".cloud_playback_key", required=required)
    return hmac.new(
        secret, ("strimkeep-play-v1:" + asset_id + ":" + nonce).encode(), hashlib.sha256
    ).hexdigest()


def link(asset_id, nonce):
    base = settings.get()["public_url"].rstrip("/")
    if not base:
        raise CloudError("请在播放设置中保存客户端可访问的服务地址", 409)
    return (
        base
        + "/api/cloud/play/"
        + asset_id
        + "?"
        + urlencode({"sig": signature(asset_id, nonce)})
    )


def root(kind="personal"):
    path = Path(settings.get()["share_output_root" if kind == "share" else "output_root"])
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise CloudError("STRM 输出目录不可见，请检查路径挂载", 409)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise CloudError("输出目录不能经过符号链接", 409)
    return path


def target(base, relative):
    parts = PurePosixPath(relative).parts
    if not parts or any(p in ("/", ".", "..") for p in parts) or "\\" in relative:
        raise CloudError("输出路径无效")
    path = base.joinpath(*parts)
    if not path.is_relative_to(base):
        raise CloudError("输出路径越界")
    for probe in (path, *path.parents):
        if probe == base.parent:
            break
        if probe.is_symlink():
            raise CloudError("拒绝输出至符号链接路径", 409)
    return path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def publish(path, data, previous=None, *, base=None):
    """No overwrite unless the persisted previous digest proves file ownership."""
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise CloudError("输出位置不是普通文件", 409)
        old = path.read_bytes()
        if old == data and previous is not None and digest(old) == previous:
            return
        if previous is None or digest(old) != previous:
            raise CloudError("目标文件已有其他内容，已保留原文件", 409)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = base if base is not None else root()
    target(base, str(path.relative_to(base)))
    temp = path.parent / (".strimkeep-" + secrets.token_hex(12))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        target(base, str(path.relative_to(base)))
        if path.exists():
            if previous is None or digest(path.read_bytes()) != previous:
                raise CloudError("输出文件在生成期间变化，已停止替换", 409)
            os.replace(temp, path)
        else:
            os.link(temp, path, follow_symlinks=False)
    finally:
        temp.unlink(missing_ok=True)


def nfo(media):
    node = ET.Element("episodedetails" if media["kind"] == "tv" else "movie")
    for key, value in (
        (
            "title",
            (
                (media.get("episode_title") or media["title"])
                if media["kind"] == "tv"
                else media["title"]
            ),
        ),
        ("showtitle", media["title"] if media["kind"] == "tv" else None),
        ("year", media["year"]),
        (
            "plot",
            (
                media.get("episode_plot")
                if media["kind"] == "tv"
                else media.get("plot", "")
            ),
        ),
        ("aired", media.get("aired") if media["kind"] == "tv" else None),
        ("season", media.get("season")),
        ("episode", media.get("episode")),
    ):
        if value is not None:
            ET.SubElement(node, key).text = str(value)
    identifier = media.get("episode_id") if media["kind"] == "tv" else media["tmdb_id"]
    if identifier:
        unique = ET.SubElement(node, "uniqueid", type="tmdb", default="true")
        unique.text = identifier
    return ET.tostring(node, encoding="utf-8", xml_declaration=True)


def fetch_sidecar(provider, row):
    """Bounded download from the same provider, with its required temporary headers."""
    import ipaddress
    import socket
    import httpx
    from urllib.parse import urlsplit
    from app.cloud.playback import safe_url

    if row["size"] > 8 * 1024 * 1024:
        raise CloudError("关联文件超过下载大小限制", 409)
    value = provider.download(row["id"], row["pickcode"], "StrimKeep-Metadata")
    url = safe_url(value["url"])
    host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(
        not ipaddress.ip_address(x[4][0]).is_global for x in addresses
    ):
        raise CloudError("关联文件下载地址解析异常", 502)
    headers = {
        k: v
        for k, v in value["headers"].items()
        if k.lower() in ("cookie", "user-agent", "referer")
    }
    headers["User-Agent"] = "StrimKeep-Metadata"
    try:
        with httpx.stream(
            "GET",
            url,
            headers=headers,
            follow_redirects=False,
            trust_env=False,
            timeout=15,
        ) as response:
            if response.status_code != 200:
                raise CloudError("关联文件下载未完成", 502)
            content = bytearray()
            for block in response.iter_bytes():
                content.extend(block)
                if len(content) > 8 * 1024 * 1024:
                    raise CloudError("关联文件超过下载大小限制", 409)
        if row["size"] and len(content) != row["size"]:
            raise CloudError("关联文件下载不完整", 502)
        return bytes(content)
    except CloudError:
        raise
    except Exception:
        raise CloudError("关联文件下载失败，可稍后重新同步", 502) from None


def fetch_artwork(poster_path):
    import re
    from urllib.request import Request
    from app import network

    if not re.fullmatch(r"/[A-Za-z0-9_.-]+\.(jpg|png|webp)", poster_path or ""):
        raise CloudError("TMDB 海报路径无效", 502)
    try:
        with network.open_external(
            Request("https://image.tmdb.org/t/p/w500" + poster_path), timeout=12
        ) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise CloudError("海报过大", 502)
        return data
    except CloudError:
        raise
    except Exception:
        raise CloudError("海报下载失败，可稍后重新同步", 502) from None


def generate(account_id, row, media, scope_cid=None, companions=None, provider=None, source_context=None):
    companions = companions or []
    if len({r["id"] for r in [row, *companions]}) != 1 + len(companions) or any(
        r["is_dir"] or r["parent_id"] != row["parent_id"] for r in companions
    ):
        raise CloudError("关联文件必须为同目录内不重复的普通文件", 409)
    share = source_context is not None
    if share and (source_context.get("source_kind") != "share" or not source_context.get("share_id")):
        raise CloudError("分享来源身份无效", 409)
    base = root("share") if share else root()
    file_key = "share:" + source_context["share_id"] + ":" + row["id"] if share else row["id"]
    def output(path, data, previous=None):
        if share:
            return publish(path, data, previous, base=base)
        return publish(path, data, previous)
    cfg = settings.get()
    relative = (
        media["directory"]
        + "/"
        + PurePosixPath(media["name"]).with_suffix(".strm").name
    )
    path = target(base, relative)
    account = store.account(account_id)
    if not account:
        raise CloudError("账号已断开", 409)
    with _LOCK:
        with store.db() as con:
            found = con.execute(
                "SELECT id FROM cloud_assets WHERE account_id=? AND file_id=? AND json_extract(payload,'$.output_root')=?",
                (account_id, file_key, str(base)),
            ).fetchall()
        if len(found) > 1:
            raise CloudError("同一源文件存在多个未明确的本地引用", 409)
        asset = store.asset(found[0][0]) if found else None
        if asset and any(
            asset["payload"]["source"][k] != row[k] for k in ("size", "sha1")
        ):
            raise CloudError(
                "已绑定来源的内容指纹变化，保留原播放引用并等待人工核对", 409
            )
        if asset and asset["state"] in ("revoked", "disconnected"):
            raise CloudError("该来源的播放引用已撤销，不自动重新启用", 409)
        asset_id = asset["id"] if asset else store.ident()
        nonce = asset["nonce"] if asset else secrets.token_hex(24)
        content = (link(asset_id, nonce) + "\n").encode()
        previous = (
            asset["payload"].get("digest")
            if asset and asset["local_path"] == str(path)
            else None
        )
        # Check conflicts before creating a publication intent.
        if (
            path.exists()
            and path.read_bytes() != content
            and (not previous or digest(path.read_bytes()) != previous)
        ):
            raise CloudError("同名 STRM 已存在，已保留原内容", 409)
        payload = {
            "uid": account["uid"],
            "source": row,
            "media": media,
            "digest": digest(content),
            "output_root": str(base),
            "root_identity": [base.stat().st_dev, base.stat().st_ino],
            "scope_cid": str(scope_cid or row["parent_id"]),
            "previous_path": asset["local_path"] if asset else "",
            "previous_digest": asset["payload"].get("digest") if asset else "",
            "previous_content": asset["payload"].get("content") if asset else "",
            "content": content.decode(),
            "owned": [],
            "warnings": [],
            "companions": companions or [],
            **(source_context or {}),
        }
        with store.db() as con:
            con.execute(
                "INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "local_path=excluded.local_path,payload=excluded.payload,state=excluded.state,updated=excluded.updated",
                (
                    asset_id,
                    account_id,
                    file_key,
                    str(path),
                    store.encode(payload),
                    nonce,
                    "publishing",
                    time.time(),
                ),
            )
        output(path, content, previous)
        if cfg["nfo_enabled"]:
            np = path.with_suffix(".nfo")
            data = nfo(media)
            old_owned = {
                x["path"]: x["digest"]
                for x in (asset or {}).get("payload", {}).get("owned", [])
            }
            try:
                output(np, data, old_owned.get(str(np)))
                payload["owned"].append({"path": str(np), "digest": digest(data)})
            except CloudError:
                payload["metadata_pending"] = True
                payload["warnings"].append("NFO 已有其他内容，保留原文件")
        old_owned = {
            x["path"]: x["digest"]
            for x in (asset or {}).get("payload", {}).get("owned", [])
        }
        for follower in companions or []:
            extension = PurePosixPath(follower["name"]).suffix.lower()
            if extension not in (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"):
                continue
            sidecar = target(
                base, str(PurePosixPath(relative).parent / follower["name"])
            )
            try:
                if provider is None:
                    raise CloudError("字幕来源连接不可用", 409)
                data = fetch_sidecar(provider, follower)
                output(sidecar, data, old_owned.get(str(sidecar)))
                payload["owned"].append(
                    {
                        "path": str(sidecar),
                        "digest": digest(data),
                        "file_id": follower["id"],
                    }
                )
            except CloudError as error:
                payload["warnings"].append(str(error))
        if cfg["artwork_enabled"] and media.get("poster_path"):
            poster = path.with_name(path.stem + "-poster.jpg")
            try:
                data = fetch_artwork(media["poster_path"])
                output(poster, data, old_owned.get(str(poster)))
                payload["owned"].append({"path": str(poster), "digest": digest(data)})
            except CloudError as error:
                payload["warnings"].append(str(error))
        if asset and asset["local_path"] != str(path):
            old = Path(asset["local_path"])
            if (
                old.is_relative_to(base)
                and not old.is_symlink()
                and old.is_file()
                and digest(old.read_bytes()) == payload["previous_digest"]
            ):
                old.unlink()
                for owned in asset["payload"].get("owned", []):
                    old_file = Path(owned["path"])
                    if (
                        old_file.is_relative_to(base)
                        and not old_file.is_symlink()
                        and old_file.is_file()
                        and digest(old_file.read_bytes()) == owned["digest"]
                    ):
                        old_file.unlink()
        with store.db() as con:
            con.execute(
                "UPDATE cloud_assets SET state='active',payload=?,updated=? WHERE id=?",
                (store.encode(payload), time.time(), asset_id),
            )
        published = store.asset(asset_id)
        from app.cloud import emby_library
        emby_library.watch(published)
        return published


def recover():
    """Recover published files after a DB/process interruption; never erase foreign files."""
    with store.db() as con:
        ids = [
            r[0]
            for r in con.execute("SELECT id FROM cloud_assets WHERE state='publishing'")
        ]
    for asset_id in ids:
        value = store.asset(asset_id)
        path = Path(value["local_path"])
        try:
            base = root("share") if value["payload"].get("source_kind") == "share" else root()
            target(base, str(path.relative_to(base)))
            good = (
                path.is_file()
                and value["payload"]["output_root"] == str(base)
                and value["payload"]["root_identity"] == [base.stat().st_dev, base.stat().st_ino]
                and digest(path.read_bytes()) == value["payload"]["digest"]
            )
        except (OSError, ValueError, CloudError):
            good = False
        with store.db() as con:
            con.execute(
                "UPDATE cloud_assets SET state=?,updated=? WHERE id=?",
                ("active" if good else "needs_review", time.time(), asset_id),
            )


def list_assets(limit=100, offset=0):
    with store.db() as con:
        rows = con.execute(
            "SELECT id,account_id,file_id,local_path,state,updated FROM cloud_assets "
            "ORDER BY updated DESC LIMIT ? OFFSET ?",
            (min(limit, 200), offset),
        ).fetchall()
    return [
        dict(zip(("id", "account_id", "file_id", "path", "state", "updated"), r))
        for r in rows
    ]


def revoke(asset_id):
    with store.db() as con:
        return (
            con.execute(
                "UPDATE cloud_assets SET state='revoked',updated=? WHERE id=?",
                (time.time(), asset_id),
            ).rowcount
            > 0
        )


def reconcile(account_id, scope_cid, seen):
    """Two complete scans, the same mounted root and exact owned bytes before prune."""
    base = root()
    identity = [base.stat().st_dev, base.stat().st_ino]
    with store.db() as con:
        ids = [
            r[0]
            for r in con.execute(
                "SELECT id FROM cloud_assets WHERE account_id=? AND state='active' AND coalesce(json_extract(payload,'$.source_kind'),'personal')='personal' AND json_extract(payload,'$.scope_cid')=? AND json_extract(payload,'$.output_root')=?",
                (account_id, str(scope_cid), str(base)),
            )
        ]
    removed = []
    deferred = []
    for asset_id in ids:
        value = store.asset(asset_id)
        payload = value["payload"]
        if value["file_id"] in seen:
            if payload.pop("missing_observations", None):
                with store.db() as con:
                    con.execute(
                        "UPDATE cloud_assets SET payload=? WHERE id=?",
                        (store.encode(payload), asset_id),
                    )
            continue
        if payload.get("root_identity") != identity:
            deferred.append(value["local_path"])
            continue
        count = int(payload.get("missing_observations", 0)) + 1
        payload["missing_observations"] = count
        if count < 2:
            with store.db() as con:
                con.execute(
                    "UPDATE cloud_assets SET payload=? WHERE id=?",
                    (store.encode(payload), asset_id),
                )
            deferred.append(value["local_path"])
            continue
        file = target(base, str(Path(value["local_path"]).relative_to(base)))
        if file.exists() and (
            not file.is_file() or digest(file.read_bytes()) != payload["digest"]
        ):
            deferred.append(str(file))
            continue
        # Source files are never deleted by synchronization.
        if [base.stat().st_dev, base.stat().st_ino] != identity:
            raise CloudError("输出目录挂载发生变化，清理已停止", 409)
        file.unlink(missing_ok=True)
        for owned in payload.get("owned", []):
            owned_file = target(base, str(Path(owned["path"]).relative_to(base)))
            if (
                owned_file.is_file()
                and digest(owned_file.read_bytes()) == owned["digest"]
            ):
                owned_file.unlink()
        with store.db() as con:
            con.execute(
                "UPDATE cloud_assets SET state='removed',payload=?,updated=? WHERE id=?",
                (store.encode(payload), time.time(), asset_id),
            )
        removed.append(str(file))
    return {"removed": removed, "deferred": deferred}
