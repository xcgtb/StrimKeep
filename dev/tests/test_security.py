# -*- coding: utf-8 -*-
"""app/security.py：会话签名 + CSRF Origin 校验（纯函数，不依赖 FastAPI）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import security as sec  # noqa: E402

KEY = b'k' * 16


def _h(**kw):
    return {k.replace('_', '-'): v for k, v in kw.items()}


class TestSession:
    def test_roundtrip(self):
        t = sec.sign_session(KEY, 'admin', 2_000_000_000)
        assert sec.verify_session(KEY, 'admin', t, now=1_000_000_000)

    def test_expired(self):
        t = sec.sign_session(KEY, 'admin', 100)
        assert not sec.verify_session(KEY, 'admin', t, now=101)

    def test_tampered_or_wrong_user_or_garbage(self):
        t = sec.sign_session(KEY, 'admin', 2_000_000_000)
        assert not sec.verify_session(KEY, 'admin', t[:-1] + ('0' if t[-1] != '0' else '1'), now=0)
        assert not sec.verify_session(b'other-key-xxxxxx', 'admin', t, now=0)
        assert not sec.verify_session(KEY, 'root', t, now=0)
        assert not sec.verify_session(KEY, 'admin', 'garbage', now=0)


class TestCsrf:
    def test_safe_methods_and_no_cookie_pass(self):
        assert not sec.csrf_blocked('GET', _h(origin='https://evil.com'), True)
        assert not sec.csrf_blocked('POST', _h(origin='https://evil.com'), False)

    def test_basic_auth_header_passes(self):
        assert not sec.csrf_blocked('POST', _h(authorization='Basic xxx', origin='https://evil.com'), True)

    def test_same_origin_passes(self):
        h = _h(origin='https://nas.local:8321', host='nas.local:8321')
        assert not sec.csrf_blocked('POST', h, True)

    def test_cross_site_blocked(self):
        h = _h(origin='https://evil.com', host='nas.local:8321')
        assert sec.csrf_blocked('POST', h, True)
        assert sec.csrf_blocked('DELETE', h, True)

    def test_null_origin_and_missing_origin_blocked(self):
        assert sec.csrf_blocked('POST', _h(origin='null', host='a'), True)
        assert sec.csrf_blocked('POST', _h(host='a'), True)

    def test_referer_fallback(self):
        assert not sec.csrf_blocked('POST', _h(referer='http://a:1/x', host='a:1'), True)
        assert sec.csrf_blocked('POST', _h(referer='http://evil.com/x', host='a:1'), True)

    def test_forwarded_host_and_allowed_origins(self):
        h = _h(origin='https://nas.example.com', host='127.0.0.1:8321', x_forwarded_host='nas.example.com')
        assert not sec.csrf_blocked('POST', h, True)
        h2 = _h(origin='https://nas.example.com', host='127.0.0.1:8321')
        assert sec.csrf_blocked('POST', h2, True)
        extra = sec.parse_allowed_origins('https://nas.example.com, other.lan:9000')
        assert not sec.csrf_blocked('POST', h2, True, extra)
        assert 'other.lan:9000' in extra

    def test_lookalike_host_not_accepted(self):
        h = _h(origin='https://nas.local.evil.com', host='nas.local')
        assert sec.csrf_blocked('POST', h, True)
