"""归属核对必须接受地址格式的历史形态，同时仍然拒绝外部篡改。"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from strimkeep.organizer import UncertainState
from strimkeep.outputs import Outputs
from strimkeep.playaddr import candidates, play_url
from strimkeep.store import Store

BASE = 'http://nas.example:9000'
PID = 'a' * 32


def build(tmp_path, content: bytes):
    store = Store(tmp_path, 'admin', 'admin-password-1')
    store.set_many({'playback_base': BASE})
    token = 'tok-legacy-1'
    stem = '测试 (2020) - 1080p'
    relative = f'影视媒体库/电影/测试 (2020) {{tmdb-1}}/{stem}.strm'
    path = tmp_path / 'strm' / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    legacy = candidates(BASE, PID, None, token)[-1]
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO output_registry VALUES(?,?,?,?,?,?,?,?,1,?)',
                         ('42', 'fid-1', '{}', '[]', PID,
                          store.crypto.encrypt(token.encode()).decode(), relative,
                          json.dumps({relative: hashlib.sha256(legacy).hexdigest()}), 0))
        store.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)',
                         (relative, '42', 'fid-1', hashlib.sha256(legacy).hexdigest()))
    engine = SimpleNamespace(root=tmp_path / 'strm', store=store)
    return store, Outputs(engine), relative, token, legacy


def test_candidates_prefer_readable_form_and_keep_legacy(tmp_path):
    forms = candidates(BASE, PID, '测试 (2020) - 1080p', 'tok')
    assert forms[0] == (BASE + '/play/' + PID + '/测试_(2020)').encode()
    assert (play_url(BASE, PID, '测试 (2020) - 1080p', 'tok') + '\n').encode() in forms
    assert (BASE + '/play/' + PID + '?token=tok\n').encode() in forms
    assert (BASE + '/play/' + PID + '/测试_(2020)_-_1080p?token=tok\n').encode() in forms


def test_guard_adopts_rebuilt_content_instead_of_blocking(tmp_path):
    store, outputs, relative, token, legacy = build(tmp_path, b'')
    # 磁盘上是新可读格式，登记的是旧格式：这正是线上 30 条 review 的形态。
    current = (play_url(BASE, PID, Path(relative).stem, token) + '\n').encode()
    (tmp_path / 'strm' / relative).write_bytes(current)
    outputs.guard('42', 'fid-1', relative)
    row = store.db.execute('SELECT digest FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone()
    assert row['digest'] == hashlib.sha256(current).hexdigest()
    files = json.loads(store.db.execute('SELECT files FROM output_registry').fetchone()['files'])
    assert files[relative] == hashlib.sha256(current).hexdigest()
    assert store.db.execute("SELECT 1 FROM events WHERE kind='output'").fetchone()


def test_guard_still_rejects_foreign_content(tmp_path):
    store, outputs, relative, token, legacy = build(tmp_path, b'')
    (tmp_path / 'strm' / relative).write_bytes(b'http://evil.example/play/other?token=x\n')
    with pytest.raises(UncertainState):
        outputs.guard('42', 'fid-1', relative)
    row = store.db.execute('SELECT digest FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone()
    assert row['digest'] == hashlib.sha256(legacy).hexdigest()


def test_guard_still_rejects_other_owner(tmp_path):
    store, outputs, relative, token, legacy = build(tmp_path, b'')
    (tmp_path / 'strm' / relative).write_bytes(
        (play_url(BASE, PID, Path(relative).stem, token) + '\n').encode())
    with pytest.raises(UncertainState):
        outputs.guard('99', 'fid-1', relative)


def test_guard_releases_stale_registration_when_file_is_gone(tmp_path):
    store, outputs, relative, token, legacy = build(tmp_path, b'')
    (tmp_path / 'strm' / relative).unlink()
    with store.db:
        store.db.execute("UPDATE owned_outputs SET account_id='99' WHERE relative_path=?", (relative,))
    outputs.guard('42', 'fid-1', relative)
    assert store.db.execute('SELECT 1 FROM owned_outputs WHERE relative_path=?', (relative,)).fetchone() is None
