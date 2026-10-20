"""Pure source selection; ambiguous mappings never authorize a filesystem path."""

def normalize(value):
    return str(value or '').strip().replace('\\', '/').rstrip('/')


def select(path, entries, field='emby_root'):
    value = normalize(path)
    if not value or '..' in value.split('/') or '\x00' in value:
        return None
    matches = [row for row in entries if row.get(field) and
               (value == normalize(row[field]) or value.startswith(normalize(row[field]) + '/'))]
    if not matches:
        return None
    size = max(len(normalize(row[field])) for row in matches)
    matches = [row for row in matches if len(normalize(row[field])) == size]
    identities = {(row['root'], row['kind']) for row in matches}
    if len(identities) != 1:
        return None
    return next((row for row in matches if row.get('origin') == 'generated'), matches[0])


def labels(paths, entries):
    return sorted({row['label'] for path in paths if (row := select(path, entries))})
