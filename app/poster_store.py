"""Best-effort, atomic, bounded poster files; never store tokens or media data."""
import hashlib
import json
import os
import re
import tempfile
import threading
import time

from . import config

MAX_BYTES = 64 * 1024 * 1024
MAX_ITEMS = 256
MAX_IMAGE = 2 * 1024 * 1024
CONTENT_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
_lock = threading.Lock()
_directory = None
_entries = {}
_NAME = re.compile(r'[0-9a-f]{64}\.img')


def _path(key):
    return config.DATA_DIR / 'poster-cache-v1' / (hashlib.sha256(key.encode()).hexdigest() + '.img')


def read(key, ttl, with_timestamp=False):
    path = _path(key)
    try:
        stat = path.stat()
        if not 0 <= time.time() - stat.st_mtime < ttl or not 0 < stat.st_size <= MAX_IMAGE + 512:
            return None
        with path.open('rb') as stream:
            header = stream.readline(512)
            if not header.endswith(b'\n'):
                return None
            meta = json.loads(header)
            data = stream.read(MAX_IMAGE + 1)
        if (meta.get('v') != 1 or meta.get('type') not in CONTENT_TYPES or
                not 0 < len(data) <= MAX_IMAGE or meta.get('size') != len(data)):
            return None
        return (data, meta['type'], stat.st_mtime) if with_timestamp else (data, meta['type'])
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def write(key, data, content_type):
    global _directory, _entries
    if content_type not in CONTENT_TYPES or not 0 < len(data) <= MAX_IMAGE:
        return
    path = _path(key)
    header = json.dumps({'v': 1, 'type': content_type, 'size': len(data)}, separators=(',', ':')).encode() + b'\n'
    payload_size = len(header) + len(data)
    temporary = None
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if _directory != path.parent:
                # One index scan on the first write, not a disk scan on every hit.
                _entries = {}
                with os.scandir(path.parent) as files:
                    for entry in files:
                        if _NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            _entries[entry.name] = (stat.st_mtime, stat.st_size)
                _directory = path.parent
            old_size = _entries.get(path.name, (0, 0))[1]
            total = sum(item[1] for item in _entries.values()) - old_size + payload_size
            count = len(_entries) + (path.name not in _entries)
            for name, (_, size) in sorted(_entries.items(), key=lambda row: row[1][0]):
                if total <= MAX_BYTES and count <= MAX_ITEMS:
                    break
                if name == path.name:
                    continue
                (path.parent / name).unlink(missing_ok=True)
                del _entries[name]
                total -= size
                count -= 1
            if total > MAX_BYTES or count > MAX_ITEMS:
                return
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='write-', suffix='.tmp', delete=False) as stream:
                temporary = stream.name
                stream.write(header)
                stream.write(data)
            os.replace(temporary, path)
            temporary = None
            _entries[path.name] = (time.time(), payload_size)
    except OSError:
        # Disk full/read-only data must not turn a valid image into a failed card.
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
