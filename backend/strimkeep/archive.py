"""Read-only archive inventory; publish complete snapshots, never infer deletion from failures."""
import json
import re
import time
from pathlib import Path

from .cloud115 import CloudError
from .media import CATEGORIES, REGIONS, KNOWN_VIDEOS as VIDEOS, NeedsReview, choose_match, parse_name
from .organize_options import file_title


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class ArchiveIndex:
    def __init__(self, engine):
        self.engine, self.store, self.cloud = engine, engine.store, engine.cloud

    def account(self, config):
        match = re.search(r'(?:^|;\s*)UID=(\d+)', self.store.get('cookie'))
        if not match or (config.get('account_id') and config['account_id'] != match[1]):
            raise CloudError('当前 115 账号无法核实，请重新登录检查。', 'LOGIN_INVALID', 401)
        return match[1]

    def latest(self, account, root):
        return self.store.db.execute("SELECT * FROM archive_snapshots WHERE account_id=? AND root_id=? AND state IN ('complete','local') ORDER BY id DESC LIMIT 1", (account, root)).fetchone()

    def summary(self, account, root):
        complete = self.latest(account, root)
        recent = self.store.db.execute('SELECT * FROM archive_snapshots WHERE account_id=? AND root_id=? ORDER BY id DESC LIMIT 1', (account, root)).fetchone()
        fields = ('id','job_id','root_id','state','created','updated','total','resolved','unresolved','unsupported')
        public = lambda row: {key: row[key] for key in fields} if row else None
        return {'snapshot': public(complete), 'latest_attempt': public(recent)}

    def insert(self, snapshot, entry, parts, hint=None, details=None, state='resolved', message=''):
        hint = hint or {}
        # Keep snapshots small and independent of playback credentials/artwork.
        original = {key: entry[key] for key in ('id','name','parent_id','size','sha1') if key in entry}
        identity = {key: details[key] for key in ('id','title','original_title','name','original_name',
                    'release_date','first_air_date') if details and key in details}
        metadata = {'original': original, 'hint': hint, 'details': identity}
        values = (snapshot, entry['id'], entry['parent_id'], entry['name'],
                  '/'.join((*parts, entry['name'])), entry['size'], entry.get('sha1', ''),
                  state, message, hint.get('media_type', ''), (details or {}).get('id'),
                  hint.get('season'), hint.get('episode'), hint.get('end_episode'), encode(metadata))
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO archive_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)

    def recount(self, snapshot):
        counts = dict(self.store.db.execute('SELECT state,count(*) FROM archive_entries WHERE snapshot_id=? GROUP BY state', (snapshot,)).fetchall())
        with self.store.db:
            self.store.db.execute('UPDATE archive_snapshots SET total=?,resolved=?,unresolved=?,unsupported=?,updated=? WHERE id=?',
                (sum(counts.values()), counts.get('resolved', 0), counts.get('unresolved', 0), counts.get('unsupported', 0), time.time(), snapshot))
        return counts

    def managed_identity(self, account, root, entry, parts):
        row = self.store.db.execute('SELECT o.metadata,i.plan,i.original,j.config FROM organized_files o JOIN organize_items i ON i.id=o.item_id JOIN organize_jobs j ON j.id=o.job_id WHERE o.file_id=?', (entry['id'],)).fetchone()
        if not row:
            return None
        config, plan, original = (json.loads(row[key]) for key in ('config','plan','original'))
        if (config.get('account_id') != account or config.get('archive_cid') != root or
                original.get('id') != entry['id'] or original.get('size') != entry['size'] or
                plan.get('target_name') != entry['name'] or plan.get('dest') != entry['parent_id'] or
                plan.get('parts') != list(parts) or
                (original.get('sha1') and entry.get('sha1') and original['sha1'].upper() != entry['sha1'].upper())):
            return None
        metadata = json.loads(row['metadata'])
        return metadata['hint'], metadata['details']

    async def identify(self, entry, parts, cache):
        hint = parse_name(entry['name'])
        # A work folder can provide identity; file season/episode always remains
        # explicit. Never map bare numbers to episodes merely from a Season folder.
        tagged = []
        for part in parts:
            if re.search(r'(?i)tmdb(?:id)?[= -]\d+', part):
                folder = parse_name(part)
                if folder['tmdb_id']:
                    tagged.append(folder)
        ids = {x['tmdb_id'] for x in tagged}
        if hint['tmdb_id']:
            ids.add(hint['tmdb_id'])
        if len(ids) > 1:
            raise NeedsReview('文件和目录声明的 TMDB ID 冲突。')
        lookup = dict(hint)
        if tagged:
            folder = tagged[-1]
            lookup.update(query=folder['query'], year=folder['year'], tmdb_id=folder['tmdb_id'])
        if not lookup['query']:
            raise NeedsReview('缺少可交叉核实的作品名称。')
        key = tuple(lookup.get(k) for k in ('media_type', 'tmdb_id', 'query', 'year'))
        if key not in cache:
            details = await self.engine.identify(lookup)
            # Even an explicit ID must agree with the folder/file's title and year.
            try:
                choose_match(lookup, [details])
            except NeedsReview:
                alt = await self.engine.tmdb(f'{lookup["media_type"]}/{details["id"]}/alternative_titles')
                enriched = {**details, 'aliases': [x.get('title', '') for x in alt.get('results', alt.get('titles', []))]}
                choose_match(lookup, [enriched])
            cache[key] = details
        details = cache[key]
        if hint['year']:
            ok, _evidence = await self.engine.year_ok(hint['media_type'], details, hint)
            if not ok:
                raise NeedsReview('文件年份与作品首映年份、季年份、该集播出年都不符。')
        # Verify episode mappings; do not probe existing archive media in this phase.
        if hint['media_type'] == 'tv':
            for number in range(hint['episode'], hint['end_episode'] + 1):
                episode = await self.engine.tmdb(f'tv/{details["id"]}/season/{hint["season"]}/episode/{number}')
                if episode.get('season_number') != hint['season'] or episode.get('episode_number') != number:
                    raise NeedsReview('已有资源的季集无法通过 TMDB 核实。')
        hint['tmdb_id'] = details['id']
        return hint, details

    async def refresh(self, job_id, config):
        account, root = self.account(config), config['archive_cid']
        self.engine.boundary(job_id)
        trail = await self.cloud.directory(root)
        with self.store.db:
            self.store.db.execute('DELETE FROM directory_cache WHERE account_id=?', (account,))
        now = time.time()
        with self.store.db:
            self.store.db.execute("UPDATE archive_snapshots SET state='interrupted',updated=? WHERE job_id=? AND state='building'", (now, job_id))
            cur = self.store.db.execute("INSERT INTO archive_snapshots(job_id,account_id,root_id,state,created,updated) VALUES(?,?,?,'building',?,?)", (job_id, account, root, now, now))
        snapshot = cur.lastrowid
        try:
            stack, seen, files, file_ids = [(root, ())], set(), [], set()
            while stack:
                self.engine.boundary(job_id)
                cid, parts = stack.pop()
                if cid in seen:
                    raise CloudError('归档目录重复出现，未发布索引。', 'INCOMPLETE_LIST')
                seen.add(cid)
                for entry in await self.cloud.children(cid):
                    self.engine.boundary(job_id)
                    if entry['is_dir']:
                        self.engine.remember_directory(job_id, cid, entry['name'], entry['id'])
                        stack.append((entry['id'], (*parts, entry['name'])))
                    elif Path(entry['name']).suffix.lower() in VIDEOS | {'.iso'}:
                        if entry['id'] in file_ids:
                            raise CloudError('归档文件 ID 重复，未发布索引。', 'INCOMPLETE_LIST')
                        file_ids.add(entry['id'])
                        files.append((entry, parts))
                self.engine.update_job(job_id, stage=f'读取归档：{len(seen)} 个目录，发现 {len(files)} 个视频')
            # Finish the entire listing before identity resolution or publishing.
            previous = self.latest(account, root)
            known = {r['file_id']: r for r in self.store.db.execute('SELECT * FROM archive_entries WHERE snapshot_id=?', (previous['id'],))} if previous else {}
            cache = {}
            for number, (entry, parts) in enumerate(files, 1):
                self.engine.boundary(job_id)
                self.engine.update_job(job_id, stage=f'核实归档身份：{number}/{len(files)} · {entry["name"]}')
                if Path(entry['name']).suffix.lower() == '.iso':
                    self.insert(snapshot, entry, parts, state='unsupported', message='ISO 本期延期，未探测或修改。')
                    continue
                old = known.get(entry['id'])
                if (old and old['state'] == 'resolved' and old['parent_id'] == entry['parent_id']
                        and old['name'] == entry['name'] and old['size'] == entry['size']
                        and old['sha1'].upper() == entry.get('sha1', '').upper()
                        and old['relative_path'] == '/'.join((*parts, entry['name']))):
                    metadata = json.loads(old['metadata'])
                    self.insert(snapshot, entry, parts, metadata['hint'], metadata['details'])
                    continue
                managed = self.managed_identity(account, root, entry, parts)
                if managed:
                    self.insert(snapshot, entry, parts, *managed)
                    continue
                try:
                    hint, details = await self.identify(entry, parts, cache)
                    self.insert(snapshot, entry, parts, hint, details)
                except NeedsReview as exc:
                    self.insert(snapshot, entry, parts, state='unresolved', message=str(exc))
            self.engine.boundary(job_id)
            self.engine.boundary(job_id)
            self.recount(snapshot)
            with self.store.db:
                self.store.db.execute("UPDATE archive_snapshots SET state='complete',updated=? WHERE id=?", (time.time(), snapshot))
            self.store.event('archive', f'任务 {job_id}：归档完整索引 {snapshot} 已发布，未修改云端文件。')
            self.store.set_many({'archive_trail:' + account + ':' + root: trail})
            return self.store.db.execute('SELECT * FROM archive_snapshots WHERE id=?', (snapshot,)).fetchone()
        except BaseException:
            # Incomplete attempts never replace the last complete snapshot.
            with self.store.db:
                self.store.db.execute("UPDATE archive_snapshots SET state='interrupted',updated=? WHERE id=? AND state='building'", (time.time(), snapshot))
            raise

    async def ensure(self, job_id, config):
        account, root = self.account(config), config['archive_cid']
        snapshot = self.latest(account, root)
        # Daily organization maintains a local catalog, never a mandatory full scan.
        if snapshot is None:
            now = time.time()
            with self.store.db:
                self.store.db.execute("INSERT INTO archive_snapshots(job_id,account_id,root_id,state,created,updated) VALUES(?,?,?,'local',?,?)", (job_id,account,root,now,now))
            snapshot = self.latest(account, root)
        return snapshot

    async def refresh_related(self, job_id, plan, original=None):
        """Read current work folders. The optional library catalog is only a hint."""
        if not plan.get('parts'):
            return
        config = json.loads(self.engine.job(job_id)['config'])
        account, root = self.account(config), config['archive_cid']
        snapshot = await self.ensure(job_id, config)
        parents = {}
        work_parts = plan['parts'][:-1] if plan['hint']['media_type'] == 'tv' else plan['parts']
        directory_reads = getattr(self.engine, '_archive_directory_reads', {})
        self.engine._archive_directory_reads = directory_reads
        # Cached listings only nominate a work directory. Its full live path is
        # always checked; missing, renamed or re-created directories fall back
        # to fresh name-by-name lookup before they can influence a decision.
        candidate = root
        for part in work_parts:
            matches = [e for e in directory_reads.get(candidate, []) if e.get('name') == part]
            if len(matches) != 1 or not matches[0].get('is_dir'):
                candidate = None
                break
            candidate = str(matches[0]['id'])
        dest = root if not work_parts else None
        if candidate is not None and work_parts:
            try:
                trail = await self.cloud.path_info(candidate)
                ids = [str(e['id']) for e in trail]
                pos = ids.index(str(root))
                names = [e['name'] for e in trail[pos + 1:]]
                if ids[-1] == str(candidate) and names == list(work_parts):
                    dest = candidate
            except CloudError as exc:
                if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                    raise
            except (KeyError, ValueError, IndexError, TypeError):
                pass
        if dest is None:
            dest = root
            for part in work_parts:
                dest = await self.engine.find_dir(job_id, dest, part)
                if dest is None:
                    break
        works = {dest: tuple(work_parts)} if dest is not None else {}
        # Reuse only within this one analysis: work and season directories are
        # otherwise listed again for naming, conflict discovery and identity.
        # Mutations and later files still perform their own fresh checks.
        work_listings = {}
        async def read_work(cid):
            if cid not in work_listings:
                work_listings[cid] = await self.cloud.children(cid)
            return work_listings[cid]
        # Discover work folders by their TMDB tag in the library's category
        # levels. No media inventory/probing of unrelated works and no manual
        # catalog is required, including after deleting the local database.
        stack, seen = [(root, ())], set()
        while stack:
            parent, parts = stack.pop()
            if parent in seen:
                continue
            seen.add(parent)
            self.engine.boundary(job_id)
            if parent not in directory_reads:
                directory_reads[parent] = await self.cloud.children(parent)
            for entry in directory_reads[parent]:
                if not entry['is_dir']:
                    continue
                relative = (*parts, entry['name'])
                tag = re.search(r'(?i)\{tmdb(?:id)?[- =](\d+)\}', entry['name'])
                if tag:
                    if int(tag[1]) == int(plan['details']['id']):
                        works[entry['id']] = relative
                    continue  # Never scan inside an unrelated work.
                if ((not parts and entry['name'] in CATEGORIES) or
                        (len(parts) in (1, 2) and re.fullmatch(r'(?:19|20)\d{2}', entry['name'])) or
                        (len(parts) == 1 and entry['name'] in {r + parts[0] for r in REGIONS})):
                    stack.append((entry['id'], relative))
        # Existing work folders are stable: do not create a second translated
        # directory after changing language preference or TMDB display title.
        if config.get('keep_existing_names', False) and len(works) == 1:
            work_id, existing_parts = next(iter(works.items()))
            season_parts = plan['parts'][-1:] if plan['hint']['media_type'] == 'tv' else []
            plan['parts'] = [*existing_parts, *season_parts]
            candidates = list(await read_work(work_id))
            if plan['hint']['media_type'] == 'tv':
                directories = sorted((v for v in candidates if v['is_dir'] and re.match(r'(?i)^Season\s+\d+', v['name'])), key=lambda v:v['name'])
                candidates = []
                for directory in directories:
                    candidates.extend(await read_work(directory['id']))
                    if any(not v['is_dir'] and Path(v['name']).suffix.lower() in VIDEOS for v in candidates):break
            if config.get('rename_enabled', True) and plan.get('target_name'):
                year_match = re.search(r'\(((?:19|20)\d{2})\)', plan['target_name'])
                year = year_match.group(1) if year_match else ''
                existing = ''
                for video in sorted(candidates, key=lambda v: v['name']):
                    if video['is_dir'] or Path(video['name']).suffix.lower() not in VIDEOS:
                        continue
                    title = file_title(video['name'])
                    if not title:
                        continue
                    # 只沿用同年份的文件名：同目录混入翻拍/续作时，不能把别人的标题搬过来。
                    found = re.search(r'\(((?:19|20)\d{2})\)|(?<!\d)((?:19|20)\d{2})(?!\d)', Path(video['name']).stem)
                    if year and found and (found.group(1) or found.group(2)) != year:
                        continue
                    existing = title
                    break
                proposed = file_title(plan['target_name'])
                if existing and proposed and 2 <= len(existing) <= 100:
                    plan['target_name'] = existing + plan['target_name'][len(proposed):]
        for work, parts in works.items():
            if plan['hint']['media_type'] == 'movie':
                parents[work] = parts
            else:
                for entry in await read_work(work):
                    season = re.match(r'(?i)^Season\s+(\d+)(?:\s+-.*)?$', entry['name'])
                    if entry['is_dir'] and season and int(season[1]) == plan['hint']['season']:
                        parents[entry['id']] = (*parts, entry['name'])
        # An older/manual catalog can point at a legacy classification. An old
        # file moved back to ingestion is removed from the cache automatically.
        related = self.store.db.execute('SELECT * FROM archive_entries WHERE snapshot_id=? AND media_type=? AND tmdb_id=?',
            (snapshot['id'], plan['hint']['media_type'], plan['details']['id'])).fetchall()
        # If the current work's parent was freshly discovered above, a fresh
        # child listing verifies all its entries in one request. A missing or
        # changed ID still takes the slower file/path lookup to detect moves.
        parent_listings = {}
        for row in related:
            self.engine.boundary(job_id)
            fid = row['file_id']
            parent = row['parent_id']
            if parent in parents and not (original and fid == original['id']):
                if parent not in parent_listings:
                    parent_listings[parent] = await read_work(parent)
                found = next((e for e in parent_listings[parent] if e['id'] == fid and not e.get('is_dir')), None)
                if found:
                    saved = json.loads(row['metadata'])['original']
                    try:
                        self.engine.check_source(saved, found, row['name'], parent)
                    except Exception:
                        pass  # Changed entries go through the existing lookup.
                    else:
                        continue
            try:
                if (original and fid == original['id']) or not await self.cloud.live_file_exists(fid):
                    raise CloudError('cached file is absent', 'FILE_NOT_FOUND', 404)
                actual = await self.cloud.file(fid)
                path = await self.cloud.path_info(actual['parent_id'])
                ids = [p['id'] for p in path]
                if root not in ids:
                    raise CloudError('cached file left archive', 'FILE_NOT_FOUND', 404)
                parents[actual['parent_id']] = tuple(p['name'] for p in path[ids.index(root)+1:])
            except CloudError as exc:
                if exc.code != 'FILE_NOT_FOUND':
                    raise
                # Cache lookup failure cannot authorize a mutation or stop a
                # different, freshly listed work directory.
                with self.store.db:
                    self.store.db.execute('DELETE FROM archive_entries WHERE snapshot_id=? AND file_id=?', (snapshot['id'], fid))
        plan['conflict_parent_ids'] = sorted(parents)
        plan['current_archive_ids'] = []
        cache = {}
        for parent, parts in parents.items():
            self.engine.boundary(job_id)
            listing = parent_listings[parent] if parent in parent_listings else await read_work(parent)
            with self.store.db:
                self.store.db.execute('DELETE FROM archive_entries WHERE snapshot_id=? AND parent_id=?', (snapshot['id'], parent))
            for entry in listing:
                if entry['is_dir'] or Path(entry['name']).suffix.lower() not in VIDEOS:
                    continue
                if original and entry['id'] == original['id']:
                    continue
                plan['current_archive_ids'].append(entry['id'])
                try:
                    identity = self.managed_identity(account, root, entry, parts) or await self.identify(entry, parts, cache)
                    self.insert(snapshot['id'], entry, parts, *identity)
                except NeedsReview as exc:
                    self.insert(snapshot['id'], entry, parts, state='unresolved', message=str(exc))
        self.recount(snapshot['id'])
        # 标识这次完整核查没有找到作品目录，仅供当前批次的复核优化使用。
        return not works

    async def candidates(self, job_id, original, plan):
        if 'current_archive_ids' not in plan:
            await self.refresh_related(job_id, plan, original)
        config = json.loads(self.engine.job(job_id)['config'])
        snapshot = self.latest(self.account(config), config['archive_cid'])
        if not snapshot:
            return []
        for parent in plan.get('conflict_parent_ids', []):
            if self.store.db.execute("SELECT 1 FROM archive_entries WHERE snapshot_id=? AND parent_id=? AND state='unresolved'", (snapshot['id'], parent)).fetchone():
                raise NeedsReview('当前作品目录有无法识别的视频，保留旧文件，本文件转待处理。')
        hint = plan['hint']
        entries = self.store.db.execute("SELECT * FROM archive_entries WHERE snapshot_id=? AND state='resolved' AND media_type=? AND tmdb_id=? AND file_id<>?", (snapshot['id'], hint['media_type'], plan['details']['id'], original['id'])).fetchall()
        result = []
        # 洗版准备对同一批「库内已有版本」会逐个复核：同一任务里重复读同一文件
        # 只会得到同一份事实（我们自己的改名/移动/回收会主动失效），用带缓存的读法。
        reader = getattr(self.cloud, 'cached_file', self.cloud.file)
        for entry in entries:
            if entry['file_id'] not in plan.get('current_archive_ids', []):
                continue
            if hint['media_type'] == 'tv' and (entry['season'] != hint['season'] or
                    max(entry['episode'], hint['episode']) > min(entry['end_episode'], hint['end_episode'])):
                continue
            self.engine.boundary(job_id)
            current = await reader(entry['file_id'])
            saved = json.loads(entry['metadata'])['original']
            self.engine.check_source(saved, current, entry['name'], entry['parent_id'])
            result.append((dict(entry), current))
        return result

    async def conflict(self, job_id, original, plan):
        if await self.candidates(job_id, original, plan):
            raise NeedsReview('当前归档目录发现同片或重叠集数；保留旧资源，新文件原名转待处理。')

    def remove_managed(self, job_id, file_id):
        config = json.loads(self.engine.job(job_id)['config'])
        snapshot = self.latest(self.account(config), config['archive_cid'])
        if snapshot:
            with self.store.db:
                self.store.db.execute('DELETE FROM archive_entries WHERE snapshot_id=? AND file_id=?', (snapshot['id'], file_id))
            self.recount(snapshot['id'])

    def record_managed(self, job_id, original, actual, plan):
        config = json.loads(self.engine.job(job_id)['config'])
        snapshot = self.latest(self.account(config), config['archive_cid'])
        if not snapshot:
            return
        entry = {**actual, 'is_dir': False}
        self.insert(snapshot['id'], entry, tuple(plan['parts']), {**plan['hint'], 'tmdb_id': plan['details']['id']}, plan['details'])
        self.recount(snapshot['id'])
