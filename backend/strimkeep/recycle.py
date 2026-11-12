"""Bind original file intents to recycle records, whose IDs are different."""
from datetime import datetime, timedelta, timezone

from .cloud115 import CloudError


def deletion_time(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (str, int, float)) and str(value).replace('.', '', 1).isdigit():
            stamp = float(value)
            return stamp / 1000 if stamp > 10**12 else stamp
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def match_record(source, action, entries):
    """Return positive, unique evidence; absence alone never means success.

    Modern intents bind a newly appeared RID to exact metadata and the saved
    pre-request snapshot. Older intents require deletion-time + parent evidence.
    Explicit original IDs are used only when that field actually exists.
    """
    exact = [e for e in entries if e.get('id') == source['id']]
    if exact:
        matches = exact
        kind = 'explicit_file_id'
    else:
        candidates = [e for e in entries if e.get('name') == source['name']
                      and e.get('size') == source['size']
                      and e.get('parent_id') == source['parent_id']
                      and e.get('is_dir') is bool(source.get('is_dir', False))]
        if 'before_rids' in action:
            matches = [e for e in candidates if e['rid'] not in action['before_rids']
                       and ((stamp := deletion_time(e.get('deleted_at'))) is None
                            or int(action.get('at', 0)) <= stamp <= action.get('at', 0) + 180)]
            kind = 'new_recycle_record'
        else:
            # Compatibility for already-submitted 0.4.2 intents: do not invent a
            # pre-request snapshot. Only a server deletion in the request window
            # can establish the missing association. Unknown time stops recovery.
            start = action.get('at')
            matches = [e for e in candidates if isinstance(start, (int,float))
                       and (stamp := deletion_time(e.get('deleted_at'))) is not None
                       and int(start) <= stamp <= start + 180]
            kind = 'legacy_intent_time'
    if not matches:
        return None
    if (len(matches) != 1 or matches[0]['name'] != source['name']
            or matches[0]['size'] != source['size']
            or (matches[0].get('parent_id') is not None and matches[0]['parent_id'] != source['parent_id'])
            or (matches[0].get('is_dir') is not None and matches[0]['is_dir'] is not bool(source.get('is_dir', False)))):
        raise CloudError('回收记录关联不唯一或名称、大小、原目录不一致，停止核查。', 'RECYCLE_UNVERIFIED', 409)
    return {**matches[0], 'match_kind': kind}
