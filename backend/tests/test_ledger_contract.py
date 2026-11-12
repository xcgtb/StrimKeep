"""契约回归：身份写在文件里，登记表只是索引（docs/LEDGER-CONTRACT.md）。

这两个断言是「以后加功能不用兼容账本」的底线：
- 所有生成 STRM 的入口产出的地址都必须能被 `identity.parse_url` 解析出身份；
- 身份比较只看身份段，改名不改变归属。
"""
from strimkeep import identity
from strimkeep.playaddr import play115_url
from strimkeep.share_service import share_play_url

SHA1 = 'AB3D5F' * 7          # 42 位十六进制，和现场一样（大写）


def test_three_documented_url_shapes_are_parseable():
    netdisk = identity.parse_url(
        play115_url('http://nas:9000', 'd0xe3f3b1qhimxlds', '3556920952132470467',
                    SHA1, 4900912877, '今天是太阳雨 S01E01.mkv'))
    assert netdisk == {'kind': 'netdisk', 'pickcode': 'd0xe3f3b1qhimxlds',
                       'file_id': '3556920952132470467', 'sha1': SHA1.upper(),
                       'size': 4900912877}

    share = identity.parse_url(share_play_url('http://nas:9000', '83c384c123c95f62',
                                              '走马上任 S01E15.mkv'))
    assert share == {'kind': 'share', 'share_id': '83c384c123c95f62'}

    playback = identity.parse_url('http://nas:9000/play/p-9f3c2a')
    assert playback == {'kind': 'playback', 'playback_id': 'p-9f3c2a'}


def test_unknown_content_is_never_guessed():
    for text in ('', '   ', 'http://nas:9000/', 'not a url',
                 'http://nas:9000/play115/share/',
                 'http://nas:9000/play115/only-two/segments',
                 'http://nas:9000/play/'):
        assert identity.parse_url(text) is None, text


def test_identity_survives_a_rename(tmp_path):
    """改名只换名称段：这正是「改名不必走账本」的依据。"""
    old = play115_url('http://nas:9000', 'pc1', '3550', SHA1, 9, '旧名字.mkv')
    new = play115_url('http://nas:9000', 'pc1', '3550', SHA1, 9, '新名字.mkv')
    assert identity.matches(old, file_id='3550') is True
    assert identity.matches(new, file_id='3550') is True
    assert identity.matches(new.replace('3550', '9999'), file_id='3550') is False
    assert identity.matches(new, file_id='3550') == identity.matches(old, file_id='3550')


def test_parse_file_handles_missing_and_broken(tmp_path):
    good = tmp_path / 'a.strm'
    good.write_text(share_play_url('', 'sid1', 'x.mkv') + '\n', encoding='utf-8')
    assert identity.parse_file(good) == {'kind': 'share', 'share_id': 'sid1'}
    assert identity.parse_file(tmp_path / 'missing.strm') is None

    broken = tmp_path / 'b.strm'
    broken.write_bytes(b'\xff\xfe\x00bad')
    assert identity.parse_file(broken) is None
