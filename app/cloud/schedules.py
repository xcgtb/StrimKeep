"""Bounded five-field numeric cron, using IANA timezones without a new dependency."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from app.cloud.errors import CloudError


def parse(expression):
    if not isinstance(expression, str) or len(expression) > 120:
        raise CloudError("Cron 应为五段数字表达式：分 时 日 月 周")
    parts = expression.split()
    if len(parts) != 5:
        raise CloudError("Cron 应为五段：分 时 日 月 周，例如 0 3 * * *")
    fields = []
    for raw, low, high in zip(parts, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
        values = set()
        for segment in raw.split(','):
            if not re.fullmatch(r'(?:\*|\d{1,2}(?:-\d{1,2})?)(?:/\d{1,2})?', segment):
                raise CloudError("Cron 仅支持数字、*、逗号、范围和步长")
            base, _, step = segment.partition('/')
            step = int(step or 1)
            if not 1 <= step <= high-low+1:
                raise CloudError("Cron 步长超出范围")
            if base == '*':
                start, end = low, high
            elif '-' in base:
                start, end = map(int, base.split('-'))
            else:
                start = int(base)
                end = high if '/' in segment else start
            if not low <= start <= end <= high:
                raise CloudError("Cron 数值超出范围")
            values.update(range(start, end+1, step))
        fields.append(values)
    fields[4] = {v % 7 for v in fields[4]}
    return parts, fields


def zone(name):
    if not isinstance(name, str) or len(name) > 80:
        raise CloudError("请选择有效时区，例如 Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise CloudError("时区无效，请填写 IANA 时区，例如 Asia/Shanghai") from None


def cron_next(expression, timezone, after):
    parts, fields = parse(expression)
    tz = zone(timezone)
    first = datetime.fromtimestamp(after, tz).date()
    minute, hour, dom, month, dow = fields
    # At most eight calendar years, including leap days, with no minute-by-minute scan.
    for offset in range(366*8):
        day = first + timedelta(days=offset)
        day_match = day.day in dom
        week_match = (day.weekday()+1) % 7 in dow
        if parts[2].startswith('*') or parts[4].startswith('*'):
            matches = day_match and week_match
        else:
            matches = day_match or week_match
        if day.month not in month or not matches:
            continue
        candidates = []
        for h in sorted(hour):
            for m in sorted(minute):
                wall = datetime(day.year, day.month, day.day, h, m)
                for fold in (0, 1):
                    stamp = wall.replace(tzinfo=tz, fold=fold).timestamp()
                    # Skip nonexistent spring-forward wall times. Both fall-back instants count.
                    if stamp > after and datetime.fromtimestamp(stamp, tz).replace(tzinfo=None) == wall:
                        candidates.append(stamp)
        if candidates:
            return min(candidates)
    raise CloudError("Cron 在未来八年内没有可执行时间，请检查日期")


def next_run(payload, now):
    mode = payload.get('schedule_mode', 'interval')
    if mode == 'cron':
        return cron_next(payload.get('cron_expression', ''), payload.get('timezone', 'Asia/Shanghai'), now)
    interval = payload.get('interval_seconds', 3600)
    if mode != 'interval' or type(interval) is not int or not 300 <= interval <= 604800:
        raise CloudError("执行间隔应为 5 分钟至 7 天")
    return now + interval
