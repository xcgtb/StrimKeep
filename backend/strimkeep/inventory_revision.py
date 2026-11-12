"""Bind a governance scan to the inventory and policy the user reviewed."""
import hashlib
import json


def governance_revision(projection, policy, settings, sources):
    works = []
    for item in projection.get('items', []):
        work = {'kind': item['kind'], 'tmdb_id': item['tmdb_id'], 'title': item['title']}
        for side in ('cloud', 'share'):
            work[side] = sorted(({'id': str(row['id']), 'path': row['path'], 'name': row['name'],
                                  'season': row.get('season'), 'episodes': row.get('episodes'),
                                  'directory_mtime': row.get('directory_mtime'),
                                  'file_size': row.get('file_size'), 'file_mtime_ns': row.get('file_mtime_ns')}
                                 for row in item[side]), key=lambda row: (row['path'], row['id']))
        works.append(work)
    value = {'complete': bool(projection.get('complete')),
             'items': sorted(works, key=lambda row: (row['kind'], row['tmdb_id'])),
             'policy': policy, 'settings': settings, 'sources': sources}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()
