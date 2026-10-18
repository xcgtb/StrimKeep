"""StrimKeep poster thumbnail variant regression tests, installed with patch."""
import io

import pytest
from fastapi import HTTPException
from app import posters
from app.routers import library


def test_card_thumbnail_is_smaller_and_cached_separately(monkeypatch):
    with posters._lock:
        posters._cache.clear()
        posters._inflight.clear()
    calls = []

    class FakeResponse(io.BytesIO):
        headers = {'Content-Type': 'image/jpeg'}

    def outbound(url, timeout):
        calls.append(url)
        return FakeResponse(b'picture-' + url.rsplit('/', 2)[-2].encode())

    monkeypatch.setattr(posters.network, 'open_external', outbound)
    thumb = library.api_tmdb_poster('one.jpg', size='w342')
    assert thumb.body == b'picture-w342'
    assert library.api_tmdb_poster('one.jpg', size='w342').body == thumb.body
    full = library.api_tmdb_poster('one.jpg')
    assert full.body == b'picture-w500'
    assert calls == [
        'https://image.tmdb.org/t/p/w342/one.jpg',
        'https://image.tmdb.org/t/p/w500/one.jpg',
    ]
    with pytest.raises(HTTPException) as invalid:
        library.api_tmdb_poster('one.jpg', size='original')
    assert invalid.value.status_code == 400
    with posters._lock:
        posters._cache.clear()
