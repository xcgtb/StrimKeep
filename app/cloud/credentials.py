"""Encrypted account credentials and a separate persistent playback key."""

import os
import secrets
import threading
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from app.cloud import store
from app.cloud.errors import CloudError

_LOCK = threading.RLock()


def key(name, *, required=False):
    base = storage_directory()
    path = base / name
    with _LOCK:
        if required and not path.exists():
            raise CloudError("账号密钥缺失，请恢复完整数据目录", 503)
        if path.is_symlink():
            raise CloudError("密钥文件不能是符号链接", 503)
        if not path.exists():
            temporary = base / (".cloud-key-" + secrets.token_hex(12))
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(Fernet.generate_key())
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary, path, follow_symlinks=False)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                value = source.read(1024)
            Fernet(value)
            return value
        except (OSError, ValueError):
            raise CloudError("账号密钥读取失败", 503) from None


def storage_directory():
    from app import storage

    base = storage._diagnostic_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base


def seal(value):
    with store.db() as con:
        required = (
            con.execute("SELECT 1 FROM cloud_accounts LIMIT 1").fetchone() is not None
        )
    return (
        Fernet(key(".cloud_credentials_key", required=required))
        .encrypt(store.encode(value).encode())
        .decode()
    )


def load(account_id):
    import json

    row = store.account(account_id)
    if not row:
        raise CloudError("网盘账号不存在", 404)
    try:
        data = Fernet(key(".cloud_credentials_key", required=True)).decrypt(
            row["sealed"].encode()
        )
        return row, json.loads(data)
    except (InvalidToken, ValueError):
        raise CloudError("账号凭据无法解密，请恢复账号密钥", 503) from None
