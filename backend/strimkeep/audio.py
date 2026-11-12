"""One representative-track policy for naming and quality comparison."""
import math

from .core import NeedsReview

DEFAULT_ORDER = ['truehd', 'dts_hd_ma', 'dts_hd_hra', 'pcm', 'flac',
                 'eac3', 'dts', 'ac3', 'aac', 'opus', 'mp3']


def representative(tracks, order=None, strict=False):
    order = DEFAULT_ORDER if order is None else order
    ranked = []
    for track in tracks:
        codec = str(track.get('codec') or '').casefold()
        profile = str(track.get('profile') or '').casefold()
        if codec == 'dts':
            if strict and profile in ('', 'none', 'unknown'):
                raise NeedsReview('DTS 音轨缺少可核实的 profile。')
            if 'ma' in profile: codec = 'dts_hd_ma'
            elif 'hra' in profile: codec = 'dts_hd_hra'
        elif codec.startswith('pcm_'): codec = 'pcm'
        channels = track.get('channels')
        valid = (isinstance(channels, (int, float)) and not isinstance(channels, bool)
                 and math.isfinite(channels) and channels > 0)
        if strict and (codec not in order or not valid):
            raise NeedsReview('音轨编码/声道缺失或未列入音频偏好。')
        # 代表音轨取的是「正片音轨」：先区分多声道（≥6）与立体声，
        # 再比编码偏好，最后才用具体声道数破同分。
        # 否则一条 2.0 的 PCM 会盖过 5.1 的 DD+——那通常只是配音轨，
        # 却会让命名和洗版比较都误判（真实案例：20 音轨资源被命名成 LPCM 2.0）。
        multichannel = 1 if (valid and channels >= 6) else 0
        score = (multichannel,
                 -order.index(codec) if codec in order else -len(order),
                 channels if valid else 0)
        ranked.append((score, {**track, 'codec': codec, 'channels': channels if valid else None}))
    if not ranked:
        if strict: raise NeedsReview('缺少可比较音轨。')
        return None
    return max(ranked, key=lambda item: item[0])


def known_representative(tracks, order=None):
    """Compare usable tracks; unknown extra streams do not invalidate known facts."""
    ranked = []
    for track in tracks:
        try:
            ranked.append(representative([track], order, strict=True))
        except NeedsReview:
            continue
    return max(ranked, key=lambda item: item[0]) if ranked else None


def naming_track(media, policy=None):
    rule = next((r for r in (policy or {}).get('rules', [])
                 if r['key'] == 'audio' and r.get('enabled')), None)
    chosen = known_representative(media.get('audio', []),
                                  (rule.get('order') or rule.get('preferences', [])) if rule else None)
    if chosen is None and rule:
        chosen = known_representative(media.get('audio', []))
    return chosen[1] if chosen else None
