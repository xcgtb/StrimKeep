"""Derive a cloud catalogue from a complete directory snapshot; no I/O."""
import re

MARK = re.compile(r'(?i)[\[{]tmdb-(\d{1,12})[\]}]')
YEAR = re.compile(r'[（(]((?:19|20)\d{2})[）)]')
SEASON = re.compile(r'(?i)^(?:Season\s*|S|第\s*)(\d{1,2})(?:\s*季)?$')


def episodes(name, inherited_season=None):
    """Return explicit episode identities, preserving S00 and multi-episode files."""
    explicit = re.search(r'(?i)S(\d{1,2})[ ._-]*E(\d{1,4})(?!\d)', name)
    if explicit:
        season, first = int(explicit[1]), int(explicit[2])
        tail = name[explicit.end():]
        numbers = [first]
        span = re.match(r'(?i)-E?(\d{1,4})(?!\d)', tail)
        if span:
            end = int(span[1])
            if first <= end <= first + 99:
                numbers = list(range(first, end + 1))
            else:
                return []
        else:
            suffix = re.match(r'(?i)(?:[ ._-]*E\d{1,4})+', tail)
            if suffix:
                numbers += [int(v) for v in re.findall(r'(?i)E(\d{1,4})', suffix[0])]
        return [(season, ep) for ep in sorted(set(numbers))]
    cn = re.search(r'第\s*(\d{1,2})\s*季.{0,8}?第\s*(\d{1,4})\s*[集话]', name)
    if cn:
        return [(int(cn[1]), int(cn[2]))]
    numbered = re.search(r'(?i)(?:^|[ ._-])EP?(\d{1,4})(?!\d)', name)
    if numbered and inherited_season is not None:
        return episodes(f'S{inherited_season:02d}E{numbered[1]}' + name[numbered.end():])
    single = (re.search(r'第\s*(\d{1,4})\s*[集话]', name) or
              re.search(r'[\[【](\d{1,4})[\]】]', name))
    return [(inherited_season, int(single[1]))] if single and inherited_season is not None else []



def collect(root, folders, files, extensions, kind_hint='auto', movie_folder='电影', tv_folder='剧集'):
    nodes = {str(row['id']): row for row in [root, *folders]}
    root_id = str(root['id'])
    movie_names = {'电影', '纪录片', movie_folder}
    tv_names = {'剧集', '电视剧', '动漫', '综艺', '儿童节目', tv_folder}
    catalogue = {}

    def chain(cid):
        result, seen = [], set()
        while cid in nodes:
            if cid in seen or len(seen) >= 128:
                raise ValueError('目录层级过深或存在循环')
            seen.add(cid)
            result.append(nodes[cid])
            if cid == root_id:
                break
            cid = str(nodes[cid]['parent_id'])
        return result

    def make(cid):
        row = nodes[cid]
        ancestry = chain(cid)
        matches = set(MARK.findall(row['name']))
        year = YEAR.search(row['name'])
        kind = kind_hint if kind_hint in ('movie', 'tv') else ''
        if not kind:
            for parent in ancestry:
                if parent['name'] in movie_names or parent['name'].endswith('电影'):
                    kind = 'movie'; break
                if parent['name'] in tv_names or parent['name'].endswith(('剧集', '电视剧', '综艺')):
                    kind = 'tv'; break
        title = YEAR.sub('', MARK.sub('', row['name'])).strip(' .-_') or row['name']
        return {'cid': cid, 'name': row['name'], 'title': title, 'year': int(year[1]) if year else 0,
                'kind': kind, 'tmdb_id': next(iter(matches)) if len(matches) == 1 else '',
                'path': '/'.join(r['name'] for r in reversed(ancestry)), 'poster': '',
                'metadata_verified': False, 'video_count': 0, 'size': 0,
                'episodes': [], 'unrecognized_episodes': 0, 'episode_videos': 0, 'files': []}

    tagged = {cid for cid, row in nodes.items() if MARK.search(row['name'])}
    for cid in tagged:
        catalogue[cid] = make(cid)
    unassigned = video_count = 0
    extensions = set(extensions)
    for row in files:
        suffix = '.' + row['name'].rsplit('.', 1)[-1].lower() if '.' in row['name'] else ''
        if suffix not in extensions:
            continue
        video_count += 1
        ancestry = chain(str(row['parent_id']))
        owner = next((str(p['id']) for p in ancestry if str(p['id']) in tagged), None)
        if owner is None:
            owner = next((str(p['id']) for p in ancestry if str(p['id']) != '0' and
                          not SEASON.fullmatch(p['name']) and p['name'] not in movie_names | tv_names), None)
        if owner is None:
            unassigned += 1
            continue
        if owner not in catalogue:
            catalogue[owner] = make(owner)
        item = catalogue[owner]
        inherited = next((int(m[1]) for parent in ancestry if (m := SEASON.fullmatch(parent['name']))), None)
        ep = episodes(row['name'], inherited) if item['kind'] != 'movie' else []
        if not item['kind'] and ep:
            item['kind'] = 'tv'
        item['video_count'] += 1
        item['size'] += int(row.get('size') or 0)
        item['episodes'].extend(ep)
        if ep:
            item['episode_videos'] += 1
        if len(item['files']) < 100:
            item['files'].append({'id': row['id'], 'name': row['name'], 'size': row.get('size', 0)})
    for item in catalogue.values():
        episode_videos = item.pop('episode_videos')
        item['unrecognized_episodes'] = item['video_count'] - episode_videos if item['kind'] == 'tv' else 0
        item['episodes'] = [list(ep) for ep in sorted(set(tuple(ep) for ep in item['episodes']))]
        item['episode_count'] = len(item['episodes'])
        item['files_truncated'] = item['video_count'] > len(item['files'])
    return {'items': sorted(catalogue.values(), key=lambda item: (item['path'], item['cid'])),
            'video_count': video_count, 'unassigned_videos': unassigned}
