"""Notification-only media labels; never change naming or quality decisions."""
import re


def version(media, source):
    fields = {**media, **media.get('naming_fields', {})}
    original = str(fields.get('original_name') or source.get('name') or '')
    raw = str(fields.get('source') or '')
    evidence = raw + ' ' + original
    if re.search(r'(?i)(?<![a-z])remux(?![a-z])', evidence):
        origin = 'BluRay Remux' if re.search(r'(?i)blu[ ._-]?ray|BDRemux', evidence) else 'Remux'
    elif re.search(r'(?i)web[ ._-]?dl', evidence):
        origin = 'WEB-DL'
    elif re.search(r'(?i)web[ ._-]?rip', evidence):
        origin = 'WEBRip'
    elif re.search(r'(?i)blu[ ._-]?ray', raw):
        origin = 'BluRay'
    else:
        origin = raw[:40]
    dynamic = str(fields.get('dynamic_range') or '')
    dynamic = re.sub(r'(?i)dolby[ ._-]*vision|dovi', 'DV', dynamic)
    resolution = str(fields.get('resolution') or '')
    codec = str(fields.get('video_codec') or fields.get('codec') or '')
    return ' · '.join(v[:40] for v in (resolution, origin, dynamic, codec) if v) or '版本未识别'


def release_group(media):
    value = str(media.get('naming_fields', {}).get('release_group') or media.get('release_group') or '').strip()
    if not value or value == '-' or len(value) > 40 or re.search(r'简|繁|字幕|中字|双语|国语|中英|(?i:chs|cht|subtitles?|CHS.ENG|CHT.ENG)', value):
        return ''
    return value
