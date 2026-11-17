"""分享文件名的识别辅助：质量标签、分类目录排除、集范围。

这些函数不读写数据库、不碰文件系统状态，独立后可直接单测，
也避免 share_service 继续承载纯解析逻辑。
"""
import re
from pathlib import Path

from .media import NeedsReview, parse_name


def quality_rank(stem):
    """文件名质量证据（分辨率标签缺失=未知 None）。保留给展示与测试；
    洗版决策已统一走 quality.compare，不再按排名淘汰。"""
    rank = {'2160p': 4, '1080p': 3, '720p': 2, '480p': 1}
    dv = {'dv': 3, 'dovi': 3, 'hdr10+': 2, 'hdr10': 2, 'hdr': 2, 'sdr': 1}
    low = str(stem).lower()
    rq = max((rank[k] for k in rank if k in low), default=0)
    rd = max((dv[k] for k in dv if k in low), default=0)
    return None if rq == 0 else (rq, rd)


def is_share_category(value):
    text = re.sub(r'[\s._/\-]+', '', str(value or '')).casefold()
    return bool(re.fullmatch(r'(?:国产|欧美|日韩|海外|外语|华语|大陆|港台|中国|英国|美国|日本|韩国|全部)?(?:电影|影片|剧集|电视剧|连续剧|动漫|动画|综艺|纪录片|影视|资源|分享|合集|网盘)', text))


def episode_range_of(stem):
    """文件名里的「季 + 集范围」；解析失败或电影返回 None（不可比，永不淘汰）。"""
    try:
        hint = parse_name(stem)
    except NeedsReview:
        return None
    return None if hint['media_type'] != 'tv' else (hint['season'], hint['episode'], hint['end_episode'])
