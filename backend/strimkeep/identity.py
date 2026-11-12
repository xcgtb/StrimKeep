"""STRM 身份：从文件内容推导，取代「查登记表才知道这是谁的」。

契约见 `docs/LEDGER-CONTRACT.md`，两条：

1. 任何生成 STRM 的代码，唯一强约束是**写出带身份的播放地址**；
2. 数据库只存**不可推导**的状态（凭据/令牌/元数据/发布意图），路径只是缓存。

现场 1078 条 STRM 全部可解析，三种形态：

    /play/<playback_id>[/<name>]                        播放令牌身份
    /play115/<pickcode>/<file_id>/<sha1>/<size>/<name>  网盘身份
    /play115/share/<share_id>/<name>                    分享身份

这就是「改名不必走账本」的依据：改名只换文件名段，身份段不变。

用法：
    identity.parse_url(text)                      # STRM 内容 → 身份
    identity.parse_file(path)                     # 本地 STRM 路径 → 身份
    identity.matches(text, file_id='3550')        # 是不是这条身份的我们的输出
"""
import re
from pathlib import Path
from urllib.parse import urlsplit

PLAYBACK = re.compile(r'^/play/(?P<playback_id>[^/]+)(?:/|$)')
NETDISK = re.compile(r'^/play115/(?P<pickcode>[^/]+)/(?P<file_id>\d+)/'
                     r'(?P<sha1>[0-9a-fA-F]{20,})/(?P<size>\d+)/')
SHARE = re.compile(r'^/play115/share/(?P<share_id>[0-9A-Za-z]+)(?:/|$)')


def parse_url(text):
    """从 STRM 内容解析身份；识别不出来返回 None（**不猜**）。"""
    if not isinstance(text, str):
        return None
    path = urlsplit(text.strip()).path or ''
    match = SHARE.match(path)
    if match:
        return {'kind': 'share', 'share_id': match.group('share_id')}
    match = NETDISK.match(path)
    if match:
        return {'kind': 'netdisk', 'pickcode': match.group('pickcode'),
                'file_id': match.group('file_id'),
                'sha1': match.group('sha1').upper(), 'size': int(match.group('size'))}
    match = PLAYBACK.match(path)
    if match:
        return {'kind': 'playback', 'playback_id': match.group('playback_id')}
    return None


def parse_file(path):
    """读本地 STRM 并解析身份；文件不在/读不了/内容不认识都返回 None。"""
    try:
        return parse_url(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError):
        return None


def matches(text, pid=None, file_id=None, share_id=None):
    """这份内容是不是「给定身份」的输出。

    只比身份段，**不比文件名**——改名不该让一份输出变成别人的。
    """
    info = parse_url(text)
    if not info:
        return False
    if pid is not None:
        return info['kind'] == 'playback' and info['playback_id'] == str(pid)
    if file_id is not None:
        return info['kind'] == 'netdisk' and info['file_id'] == str(file_id)
    if share_id is not None:
        return info['kind'] == 'share' and info['share_id'] == str(share_id)
    return False
