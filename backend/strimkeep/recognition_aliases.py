"""Exact title corrections learned from explicit manual matches."""
import time
import unicodedata

from .media import NeedsReview, parse_name, require_single_episode, require_work_identity


PUBLIC_FIELDS = ('id', 'alias', 'media_type', 'source_year', 'tmdb_id', 'title', 'target_year', 'created')
PUBLIC_COLUMNS = ', '.join(PUBLIC_FIELDS)


def alias_key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def candidate(original):
    """Find the same title and episode evidence the automatic scanner would see."""
    name = original.get('name') or ''
    if not name or original.get('split_reason'):
        return None
    try:
        hint = require_work_identity(require_single_episode(parse_name(name, original.get('folder')), name))
    except NeedsReview:
        return None
    if hint.get('tmdb_id') or not alias_key(hint.get('query') or ''):
        return None
    return {'alias': hint['query'], 'media_type': hint['media_type'], 'source_year': hint.get('year') or 0,
            'season': hint.get('season'), 'episode': hint.get('episode')}


def remember(store, source, target):
    """Persist in the same transaction as the manual job; reject a conflicting rule."""
    key = (source['media_type'], alias_key(source['alias']), source['source_year'])
    existing = store.db.execute(
        'SELECT tmdb_id FROM recognition_aliases WHERE media_type=? AND alias_key=? AND source_year=?', key).fetchone()
    if existing:
        if existing['tmdb_id'] != target['tmdb_id']:
            raise ValueError(f'这条片名已有纠正规则，指向 TMDB {existing["tmdb_id"]}。请先在「识别测试 → 识别纠正」移除旧规则。')
        return
    store.db.execute(
        'INSERT INTO recognition_aliases(alias,media_type,alias_key,source_year,tmdb_id,title,target_year,created) '
        'VALUES(?,?,?,?,?,?,?,?)', (source['alias'], *key, target['tmdb_id'], target['query'], target['year'], time.time()))


def apply_alias(store, hint):
    """Use a confirmed alias only when parsing did not yield an explicit TMDB ID."""
    query = alias_key(hint.get('query') or '')
    if hint.get('tmdb_id') or not query:
        return hint
    row = store.db.execute(
        f'SELECT {PUBLIC_COLUMNS} FROM recognition_aliases WHERE media_type=? AND alias_key=? AND source_year=?',
        (hint['media_type'], query, hint.get('year') or 0)).fetchone()
    if row:
        hint.update(tmdb_id=row['tmdb_id'], identity_source='saved_alias', recognition_alias=dict(row))
    return hint


def scan_hint(store, name, folder=None):
    return apply_alias(store, require_single_episode(parse_name(name, folder), name))
