# -*- coding: utf-8 -*-
"""契约：飞牛媒体探测拦截的匹配规则。

这条一旦放宽，Emby 自己的播放请求会被一起挡掉；一旦写错版本号，拦截永久失效。
两边都不能靠猜。
"""
from app.routers.cloud import FEINIU_PROBE_UA, is_probe_request


def test_matches_exact_version():
    assert is_probe_request('Lavf/60.3.100') is True
    assert is_probe_request('Mozilla/5.0 Lavf/60.3.100 (compatible)') is True


def test_match_is_case_insensitive():
    assert is_probe_request('LAVF/60.3.100') is True


def test_other_ffmpeg_versions_are_not_blocked():
    """只挡 60.3.100：别的版本可能是正常播放器。"""
    assert is_probe_request('Lavf/58.76.100') is False
    assert is_probe_request('Lavf/60.16.100') is False


def test_emby_and_browser_are_not_blocked():
    assert is_probe_request('Emby/4.8.0') is False
    assert is_probe_request('Mozilla/5.0 (iPhone) Safari') is False
    assert is_probe_request('') is False
    assert is_probe_request(None) is False


def test_switch_off_disables_blocking():
    assert is_probe_request('Lavf/60.3.100', False) is False


def test_constant_is_the_upstream_value():
    assert FEINIU_PROBE_UA == 'Lavf/60.3.100'
