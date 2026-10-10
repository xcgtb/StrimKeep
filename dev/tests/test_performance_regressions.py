"""Perf invariants: preserve fail-closed scans and deduplicate poster I/O."""
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from app import lib, posters


def test_scan_parses_shared_directory_identity_once(tmp_path, monkeypatch):
    root = tmp_path / 'share'
    show = root / '剧集' / '节目 (2026) {tmdb-123}' / 'Season 01'
    show.mkdir(parents=True)
    for n in range(1, 31):
        (show / f'节目.S01E{n:02}.strm').write_text('https://example.invalid/video')
    parser = lib.governance_title_key
    calls = []

    def counted(title):
        calls.append(title)
        return parser(title)

    monkeypatch.setattr(lib, 'governance_title_key', counted)
    monkeypatch.setattr(lib, '_eng', lambda: SimpleNamespace(
        db_media_index_load=lambda _: {},
        db_media_index_upsert=lambda *_: None,
        db_media_index_delete_missing=lambda *_: None,
        _CATEGORY_NAMES={'剧集'}))
    got = lib.Lib(root)
    assert got.strm_count == 30
    assert len(calls) == 1
    assert list(got.key_tmdb_tv.values()) == ['123']
    assert sum(len(files) for seasons in got.tv.values() for files in seasons.values()) == 30
    # A linked STRM never becomes a media candidate (fail closed).
    (show / 'link.strm').symlink_to(show / '节目.S01E01.strm')
    import pytest
    with pytest.raises(lib.LibraryScanError, match='软链接'):
        lib.Lib(root)


def test_tmdb_poster_single_flight_deduplicates_concurrent_downloads(monkeypatch):
    with posters._lock:
        posters._cache.clear()
        posters._inflight.clear()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def outbound(url, timeout):
        calls.append(url)
        entered.set()
        assert release.wait(5), 'timed out waiting for test release'
        res = io.BytesIO(b'poster-content')
        res.headers = {'Content-Type': 'image/jpeg'}
        return res

    monkeypatch.setattr(posters.network, 'open_external', outbound)
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(posters.get_tmdb_poster, 'single-flight.jpg')
        assert entered.wait(3)
        others = [pool.submit(posters.get_tmdb_poster, 'single-flight.jpg') for _ in range(7)]
        # The leader remains blocked until all 8 requests have been scheduled.
        release.set()
        answers = [first.result(timeout=5)] + [f.result(timeout=5) for f in others]
    assert answers == [(b'poster-content', 'image/jpeg')] * 8
    assert len(calls) == 1
    with posters._lock:
        assert not posters._inflight
        posters._cache.clear()


def test_tmdb_poster_failed_flight_is_retriable(monkeypatch):
    with posters._lock:
        posters._cache.clear()
        posters._inflight.clear()
    calls = []

    def outbound(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError('offline')
        result = io.BytesIO(b'back-online')
        result.headers = {'Content-Type': 'image/webp'}
        return result

    monkeypatch.setattr(posters.network, 'open_external', outbound)
    import pytest
    with pytest.raises(TimeoutError):
        posters.get_tmdb_poster('retry.jpg')
    assert posters.get_tmdb_poster('retry.jpg') == (b'back-online', 'image/webp')
    assert len(calls) == 2
    with posters._lock:
        assert not posters._inflight
        posters._cache.clear()
