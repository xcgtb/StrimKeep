"""Bounded scan delivery and path-specific library checks outside the FIFO worker.

Refresh requests are repeatable; cloud file mutations are never replayed here.
An indexed STRM is not proof of successful playback.
"""
import asyncio
import random
import json
import re
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx
from .playaddr import parse_play_path
from .store import digest
from .network import emby_request
from .native_media_info import NativeMediaInfo


class EmbyDelivery:
    MAX_ATTEMPTS = 3
    CHECK_SECONDS = 10
    FIRST_CHECK_SECONDS = 3
    DEADLINE_SECONDS = 1800
    LIBRARY_SECONDS = 600
    # Emby 自带的 LibraryMonitor 通常先响应新增路径；只有在核对窗口里一直
    # 没收录时才补一次目标库刷新，避免每批都扫库。
    SCAN_AFTER_SECONDS = 120
    SCAN_COOLDOWN_SECONDS = 300
    SCAN_MAX_PER_JOB = 2
    SCAN_RETRY_SECONDS = 60
    # 媒体库路径自检（0.8.19）：启动 60 秒后第一次，之后每 6 小时看一次；
    # 结论每天最多写一条，避免把操作记录刷成墙。
    COVERAGE_START_DELAY = 60
    COVERAGE_INTERVAL = 6 * 3600

    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.gate = asyncio.Lock()
        self.task = None
        self.media_info = NativeMediaInfo(self)
        self.libraries = {}
        self.scan_windows = {}
        # emby_outbox 也由 store 启动 schema 建好；这里保留幂等 DDL，因为交付器会被
        # 单独实例化（测试/后台），不能假定 store 的 schema 一定先跑过。
        with self.store.db:
            self.store.db.execute('CREATE TABLE IF NOT EXISTS emby_outbox(job_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1, next_due REAL NOT NULL DEFAULT 0)')

    @property
    def http(self):
        return getattr(self, '_http', self.engine.http)

    @http.setter
    def http(self, client):
        self._http = client

    def connection(self):
        from .emby_connection import saved_base
        return saved_base(self.store)

    async def library_folders(self, url, key):
        """Emby 媒体库根路径，10 分钟缓存。取不到时返回 None，表示本次不做预检。"""
        cached = self.libraries.get(url)
        now = time.time()
        if cached and now - cached[0] < self.LIBRARY_SECONDS:
            return cached[1]
        try:
            response = await emby_request(self.http, 'GET', url + '/Library/VirtualFolders',
                                          headers={'X-Emby-Token': key}, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            # 预检只是排障手段：任何传输/解析问题都不能影响真正的通知与交付。
            return None
        if not isinstance(payload, list):
            return None
        folders = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            locations = [x for x in (item.get('Locations') or []) if isinstance(x, str) and x.strip()]
            if locations:
                options = item.get('LibraryOptions') if isinstance(item.get('LibraryOptions'), dict) else {}
                folders.append({'name': str(item.get('Name') or ''),
                                'locations': [x.rstrip('/') for x in locations],
                                'id': str(item.get('ItemId') or ''),
                                'type': str(item.get('CollectionType') or options.get('ContentType') or '')})
        self.libraries[url] = (now, folders)
        return folders

    async def library_sizes(self, url, key, ids):
        """每个库的条目数，10 分钟缓存。只用来在等价库之间挑选更轻的目标。"""
        now = time.time()
        cached = getattr(self, '_sizes', None)
        if cached and cached[0] == url and now - cached[1] < self.LIBRARY_SECONDS:
            sizes = dict(cached[2])
        else:
            sizes = {}
        wanted = [i for i in ids if i and i not in sizes]
        if wanted:
            headers = {'X-Emby-Token': key}
            for lib_id in wanted:
                try:
                    response = await emby_request(self.http, 'GET', url + '/Items', headers=headers,
                                                  params={'ParentId': lib_id, 'Recursive': 'true',
                                                          'Limit': 1, 'Fields': ''}, timeout=10)
                    response.raise_for_status()
                    total = response.json().get('TotalRecordCount')
                    sizes[lib_id] = int(total) if isinstance(total, int) else None
                except Exception:
                    sizes[lib_id] = None
        self._sizes = (url, now, sizes)
        return sizes

    @staticmethod
    def covering(folders, path):
        """返回覆盖该路径的 (媒体库, 命中的库根) 列表，按库根从长到短。"""
        result = []
        for folder in folders:
            for location in folder['locations']:
                if path == location or path.startswith(location + '/'):
                    result.append((folder, location))
                    break
        result.sort(key=lambda pair: len(pair[1]), reverse=True)
        return result

    @staticmethod
    def covered_by(folders, path):
        """返回覆盖该路径的媒体库列表。路径不在任何库根下时为空。"""
        return [folder for folder, _ in EmbyDelivery.covering(folders, path)]

    async def coverage_selfcheck(self, now=None):
        """媒体库路径自检：STRM 写在哪里，Emby 的库就必须能看见那里。

        为什么需要：0.6.5 现场踩过的坑——账号、STRM、通知、逐条核对全对，只有
        Emby 的库路径还指着改名前的旧目录，于是永远扫不到，要等 30 分钟核对超时
        才报「未确认」。这条自检把发现时间压到启动后 60 秒，并直接说明是路径问题。

        只读、每天最多一次、失败不下结论：
        - 只读 `GET /Library/VirtualFolders`（本方法依赖的 library_folders 自带 10 分钟缓存）；
        - 未配置 Emby、或读不到库信息（网络/权限/版本问题）时返回 None，
          绝不把「读不到」写成「配错了」；
        - 结论写 meta（`emby_coverage_last`）与操作记录，日志中心与总览都能看到。
        """
        now = time.time() if now is None else now
        connection = self.connection()
        if not connection:
            return None
        today = time.strftime('%Y-%m-%d', time.localtime(now))
        # 用 meta_json 读：set_meta() 写的是 JSON 字符串（带引号），直接比原值永远不等。
        if self.store.meta_json('emby_coverage_day', '') == today:
            return self.store.meta_json('emby_coverage_last')
        folders = await self.library_folders(connection[0], connection[1])
        if folders is None:
            return None
        root = str(self.store.get('console_config', {}).get('emby_strm_path') or '/strimkeep').rstrip('/')
        # 「库根落在 STRM 目录内」才算覆盖：README 推荐的配置本来就是把库指向
        # /strimkeep/影视媒体库/电影 这类子目录，不能要求库根必须等于 /strimkeep。
        inside = [folder for folder in folders
                  if any(loc == root or loc.startswith(root + '/') for loc in folder['locations'])]
        report = {'at': now, 'root': root, 'covered': bool(inside),
                  'libraries': [folder['name'] for folder in inside],
                  'paths': sorted({loc for folder in folders for loc in folder['locations']})[:20]}
        self.store.set_meta('emby_coverage_day', today)
        self.store.set_meta('emby_coverage_last', report)
        if inside:
            self.store.event('emby', '媒体库路径自检通过：%s 已被媒体库「%s」覆盖。'
                             % (root, '、'.join(report['libraries'][:3])))
        else:
            self.store.event('emby', '媒体库路径自检未通过：没有任何媒体库指向 %s；'
                                     'STRM 即使生成成功也不会入库，请把 Emby 媒体库改到该目录下。'
                                     '当前库根：%s。'
                             % (root, '、'.join(report['paths'][:5]) or '无'))
        return report

    async def coverage_loop(self):
        """周期跑 coverage_selfcheck：启动 60 秒后第一次，之后每 6 小时一次。

        延迟启动是刻意的：正常启动不因为多一次 HTTP 变慢，测试里的短生命周期
        进程也根本走不到这里（不会给假 Emby 发请求）。取消时立即退出。
        """
        await asyncio.sleep(self.COVERAGE_START_DELAY)
        while not getattr(self.engine, 'closing', False):
            try:
                await self.coverage_selfcheck()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.store.event('emby', '媒体库路径自检失败，下个周期继续。')
            await asyncio.sleep(self.COVERAGE_INTERVAL)

    def report_overlap(self, jid, folders):
        """库根互相包含时同一文件会被重复入库，仅告警，不改配置。"""
        flat = [(f['name'], loc) for f in folders for loc in f['locations']]
        conflicts = []
        for index, (name, path) in enumerate(flat):
            for other_name, other in flat[index + 1:]:
                if name == other_name:
                    continue
                if path.startswith(other + '/') or other.startswith(path + '/'):
                    conflicts.append(f'{name} 与 {other_name} 路径重叠')
        if conflicts:
            self.store.event('emby', f'任务 {jid}：媒体库根路径重叠，同一作品可能重复入库（'
                                     + '；'.join(sorted(set(conflicts))[:3]) + '）。')

    async def coverage_problem(self, jid, payload):
        """核对不通过时，判断目标路径是否根本不在任何媒体库下。"""
        connection = self.connection()
        if not connection:
            return ''
        url, key = connection
        folders = await self.library_folders(url, key)
        if folders is None:
            return ''
        self.report_overlap(jid, folders)
        pending = [t for t in payload.get('targets', []) if not t.get('confirmed') and t.get('emby_path')]
        missing = [t['emby_path'] for t in pending if not self.covered_by(folders, t['emby_path'])]
        if not missing:
            return ''
        sample = '；'.join(missing[:3])
        more = f'（另 {len(missing) - 3} 条）' if len(missing) > 3 else ''
        return (f'{len(missing)} 条 STRM 不在任何 Emby 媒体库下，扫描不会收录它们：{sample}{more}。'
                '请把这些分类目录加入 Emby 媒体库后再同步。')

    async def scan_target(self, payload):
        """为未入库路径挑选唯一可确定的目标媒体库。

        只有全部待入库路径都由同一个库、且覆盖它们的库根完全一致、类型一致时
        才返回该库；其余情况返回原因，宁可不扫也不猜。
        """
        connection = self.connection()
        if not connection:
            return None, '未配置 Emby 连接'
        url, key = connection
        folders = await self.library_folders(url, key)
        if folders is None:
            return None, '媒体库列表读取失败'
        pending = [t for t in payload.get('targets', []) if not t.get('confirmed') and t.get('emby_path')]
        if not pending:
            return None, ''
        selected = {}
        for target in pending:
            covers = self.covering(folders, target['emby_path'])
            if not covers:
                return None, '有 STRM 不在任何 Emby 媒体库下'
            names = {folder['name'] for folder, _ in covers}
            if len({folder.get('type') or '' for folder, _ in covers}) > 1:
                return None, '覆盖该路径的媒体库类型不一致：' + '、'.join(sorted(names))
            if len({root for _, root in covers}) > 1:
                return None, '媒体库根路径重叠且不一致：' + '、'.join(sorted(names))
            if not all(folder.get('id') for folder, _ in covers):
                return None, '媒体库缺少条目 ID，无法请求刷新：' + '、'.join(sorted(names))
            # 多个媒体库可以共用同一个库根（例如正式库和专用测试库指向同一目录）。
            # 刷新其中任意一个都会重新读取同一个目录，按条目数从少到多、再按
            # 条目 ID 取一个：结果一致，但优先动更轻的库。
            candidates = [folder for folder, _ in covers]
            sizes = await self.library_sizes(url, key, [f['id'] for f in candidates])
            chosen = min(candidates, key=lambda f: (sizes.get(f['id']) if sizes.get(f['id']) is not None else 10 ** 9,
                                                    int(f['id']) if f['id'].isdigit() else 0))
            selected[chosen['id']] = chosen
        if len(selected) != 1:
            return None, '未入库路径分属多个媒体库：' + '、'.join(sorted(f['name'] for f in selected.values()))
        return next(iter(selected.values())), ''

    async def request_scan(self, jid, library, now):
        """刷新单个媒体库。返回 (是否被接受, 失败原因)。"""
        connection = self.connection()
        if not connection:
            return False, '未配置 Emby 连接'
        url, key = connection
        try:
            response = await emby_request(self.http, 'POST', url + f'/Items/{library["id"]}/Refresh',
                                          headers={'X-Emby-Token': key},
                                          params={'Recursive': 'true', 'ImageRefreshMode': 'Default',
                                                  'MetadataRefreshMode': 'Default'}, timeout=20)
            if response.status_code >= 400:
                return False, f'HTTP {response.status_code}'
        except httpx.HTTPError as exc:
            detail = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            return False, str(detail)
        self.scan_windows[library['id']] = now
        return True, ''

    async def maybe_scan(self, jid, payload, now):
        """核对一段时间仍未入库时补一次目标库刷新；结论仍由逐条核对决定。"""
        if len(payload.get('scans') or []) >= self.SCAN_MAX_PER_JOB:
            return False
        sent = payload.get('sent_at') or payload.get('ready_at') or now
        if now - sent < self.SCAN_AFTER_SECONDS:
            return False
        last_attempt = payload.get('scan_attempt_at') or 0
        if last_attempt and now - last_attempt < self.SCAN_RETRY_SECONDS:
            return False
        library, reason = await self.scan_target(payload)
        if not library:
            if reason and payload.get('scan_reason') != reason:
                payload['scan_reason'] = reason
                self.save(jid, payload=json.dumps(payload, ensure_ascii=False))
                self.store.event('emby', f'任务 {jid}：未请求媒体库刷新——{reason}。')
            return False
        pending = sum(1 for t in payload.get('targets', []) if not t.get('confirmed'))
        last = self.scan_windows.get(library['id'], 0)
        if now - last < self.SCAN_COOLDOWN_SECONDS:
            if payload.get('scan_reason') != 'cooling':
                payload['scan_reason'] = 'cooling'
                self.save(jid, payload=json.dumps(payload, ensure_ascii=False))
                self.store.event('emby', f'任务 {jid}：媒体库「{library["name"]}」刚刷新过（{int(now - last)} 秒前），本次不重复请求。')
            return False
        ok, detail = await self.request_scan(jid, library, now)
        payload = json.loads(self.row(jid)['payload'])
        payload['scan_attempt_at'] = now
        if not ok:
            payload['scan_reason'] = '刷新失败：' + detail
            self.save(jid, payload=json.dumps(payload, ensure_ascii=False))
            self.store.event('emby', f'任务 {jid}：请求刷新媒体库「{library["name"]}」失败（{detail}），保留待核查。')
            return False
        payload.setdefault('scans', []).append({'library': library['name'], 'id': library['id'],
                                                'at': now, 'pending': pending})
        payload['scan_reason'] = ''
        self.save(jid, state='checking', payload=json.dumps(payload, ensure_ascii=False),
                  next_due=now + self.CHECK_SECONDS,
                  message=f'Emby 已接受路径通知，已确认入库 '
                          f'{sum(1 for t in payload.get("targets", []) if t.get("confirmed"))}/'
                          f'{len(payload.get("targets", []))}；已请求刷新媒体库「{library["name"]}」，继续逐条核对。')
        self.engine.update_job(jid, notify_state='checking')
        self.store.event('emby', f'任务 {jid}：{int(now - sent)} 秒仍未收录 {pending} 条路径，已请求刷新媒体库'
                                 f'「{library["name"]}」；刷新被接受不代表已入库，继续逐条核对。')
        return True

    def targets(self, jid, only_ids=None):
        result,seen = [],set()
        # 0.8.20：任务配置在循环外取一次。以前每个目标都要查一次 organize_jobs
        # 再解析一遍 config JSON（一季 24 集就是 24 次多余的查询）。
        job_config = json.loads(self.engine.job(jid)['config'])
        for row in self.store.db.execute("SELECT i.id,i.file_id,i.plan,i.state FROM organize_items i WHERE job_id=? AND state IN ('done','recycled') ORDER BY id", (jid,)):
            if only_ids is not None and row['id'] not in only_ids:
                continue
            plan = json.loads(row['plan'])
            if plan.get('strm_filtered'):continue
            update_type='Created'
            if row['state']=='recycled':
                proof=plan.get('wash',{}).get('duplicate',{})
                if not any(s.get('ready') for s in proof.get('subtitles',[])):continue
                # A duplicate may contribute subtitles to an existing movie.
                # Notify that managed movie instead of falling back to a global scan.
                managed=self.store.db.execute('SELECT plan FROM organize_items WHERE id=?',(proof.get('managed_item_id'),)).fetchone()
                if not managed:continue
                plan=json.loads(managed['plan']);update_type='Modified'
            if not plan.get('relative_path') or not plan.get('playback_id'): continue
            identity=(plan['relative_path'],plan['playback_id'])
            if identity in seen:continue
            seen.add(identity)
            if job_config.get('job_kind') == 'strm_sync' and not plan.get('output_changed'): continue
            hint, details = plan.get('hint', {}), plan.get('details', {})
            if not hint:
                managed = self.store.db.execute('SELECT metadata FROM organized_files WHERE file_id=?', (row['file_id'],)).fetchone()
                metadata = json.loads(managed['metadata']) if managed else {}
                hint, details = metadata.get('hint', {}), metadata.get('details', {})
            relative = plan['relative_path']
            folder = PurePosixPath(relative).parent.name
            if folder.startswith('Season '): folder = PurePosixPath(relative).parent.parent.name
            title = details.get('title') or details.get('name') or re.split(r'\s*\(\d{4}\)|\s*\{tmdb-', folder)[0]
            mapping=self.store.get('console_config',{})
            emby_path=str(PurePosixPath(mapping.get('emby_strm_path','/strimkeep'))/relative)
            result.append({'emby_path':emby_path,'update_type':update_type,'relative':relative, 'pid':plan['playback_id'], 'kind':hint.get('media_type'),
                           'tmdb':details.get('id') or hint.get('tmdb_id'), 'title':title, 'confirmed':False})
        return result

    def row(self, jid):
        return self.store.db.execute('SELECT * FROM emby_deliveries WHERE job_id=?', (jid,)).fetchone()

    def save(self, jid, **values):
        if 'payload' in values:
            # 0.8.20：payload 一变就同步「总数 / 已确认数」摘列，任务列表与 SSE 因此
            # 不需要再解析整段 JSON（原来 30 个任务 × 每 2 秒各解析一次）。
            try:
                targets = json.loads(values['payload']).get('targets') or []
            except (TypeError, ValueError):
                targets = []
            values['total'] = len(targets)
            values['confirmed'] = sum(1 for t in targets if isinstance(t, dict) and t.get('confirmed'))
        with self.store.db:
            self.store.db.execute('UPDATE emby_deliveries SET '+','.join(k+'=?' for k in values)+' WHERE job_id=?', (*values.values(), jid))

    def updates(self, jid, payload):
        mapping=self.store.get('console_config',{}).get('emby_strm_path','/strimkeep')
        changed={t.get('emby_path',str(PurePosixPath(mapping)/t['relative'])):t.get('update_type','Created') for t in payload['targets']}
        config=json.loads(self.engine.job(jid)['config'])
        for relative in config.get('emby_deleted_paths',[]):
            changed.setdefault(str(PurePosixPath(mapping)/relative),'Deleted')
        for entry in self.store.db.execute("SELECT path FROM strm_changes WHERE job_id=? AND action='removed'",(jid,)):
            changed.setdefault(str(PurePosixPath(mapping)/entry['path']),'Deleted')
        return [{'Path':p,'UpdateType':kind} for p,kind in sorted(changed.items())]

    def enqueue(self, jid):
        # Intent is durable before the organizer continues. A revision protects
        # events added while an earlier delivery is being prepared.
        with self.store.db:
            self.store.db.execute('INSERT INTO emby_outbox(job_id,revision) VALUES(?,1) ON CONFLICT(job_id) DO UPDATE SET revision=revision+1,next_due=0', (jid,))
        if not self.row(jid):
            self.engine.update_job(jid, notify_state='retrying')

    async def drain_outbox(self):
        row = self.store.db.execute('SELECT job_id,revision FROM emby_outbox WHERE next_due<=? ORDER BY job_id LIMIT 1', (time.time(),)).fetchone()
        if not row:
            return
        if not self.engine.job(row['job_id']):
            with self.store.db:
                self.store.db.execute('DELETE FROM emby_outbox WHERE job_id=?', (row['job_id'],))
            return
        try:
            await self.submit(row['job_id'])
        except Exception as exc:
            with self.store.db:
                self.store.db.execute('UPDATE emby_outbox SET next_due=? WHERE job_id=? AND revision=?', (time.time()+10,row['job_id'],row['revision']))
            self.store.event('emby', f'任务 {row["job_id"]}：路径通知入队待重试（{type(exc).__name__}）。')
            return
        with self.store.db:
            self.store.db.execute('DELETE FROM emby_outbox WHERE job_id=? AND revision=?', (row['job_id'],row['revision']))

    async def submit(self, jid):
        # The same job can publish more paths while earlier ones are being
        # verified. Serialize with tick and retain the previous evidence.
        async with self.gate:
            now = time.time()
            row = self.row(jid)
            item_ids = {r['id'] for r in self.store.db.execute("SELECT id FROM organize_items WHERE job_id=? AND state IN ('done','recycled')", (jid,))}
            if not row:
                targets = self.targets(jid)
                job_row = self.engine.job(jid)
                is_share = json.loads(job_row['config']).get('job_kind') == 'share_organize'
                if job_row['notify_state'] not in ('not_started', 'retrying', 'not_needed') and not (is_share and job_row['notify_state'] == 'accepted'):
                    return
                connection = self.connection()
                payload = {'targets': targets, 'connection': digest('|'.join(connection)) if connection else '', 'ready_at': now}
                payload['updates'] = self.updates(jid, payload)
                payload['handled_deletions'] = sorted(u['Path'] for u in payload['updates'] if u['UpdateType']=='Deleted')
                payload['handled_items'] = sorted(item_ids)
                if not payload['updates']:
                    self.engine.update_job(jid, notify_state='not_needed')
                    return
                with self.store.db:
                    self.store.db.execute('INSERT OR IGNORE INTO emby_deliveries(job_id,state,payload,next_due,deadline) VALUES(?,?,?,?,?)',
                                          (jid, 'pending', json.dumps(payload, ensure_ascii=False), now, now+self.DEADLINE_SECONDS))
            else:
                payload = json.loads(row['payload'])
                handled = set(payload.get('handled_items', []))
                new_ids = item_ids - handled
                known_deletions = set(payload.get('handled_deletions', [u['Path'] for u in payload.get('updates', []) if u.get('UpdateType') == 'Deleted']))
                pending_deletions = [u for u in self.updates(jid, {'targets': []}) if u['UpdateType'] == 'Deleted' and u['Path'] not in known_deletions]
                if not new_ids and not pending_deletions:
                    return
                payload['handled_items'] = sorted(handled | new_ids)
                candidates = self.targets(jid, new_ids)
                known = {(t['relative'], t['pid']): t for t in payload.get('targets', [])}
                fresh = []
                for target in candidates:
                    key = (target['relative'], target['pid'])
                    if key not in known:
                        payload.setdefault('targets', []).append(target)
                        known[key] = target
                        fresh.append(target)
                    elif target.get('update_type') == 'Modified':
                        known[key]['confirmed'] = False
                        fresh.append(target)
                if not fresh and not pending_deletions:
                    self.save(jid, payload=json.dumps(payload, ensure_ascii=False))
                    return
                fresh_updates = [u for u in self.updates(jid, {'targets': fresh}) if u['UpdateType'] != 'Deleted'] + pending_deletions
                payload['handled_deletions'] = sorted(known_deletions | {u['Path'] for u in pending_deletions})
                # If delivery is not confirmed, retain its pending paths as well.
                old_updates = payload.get('updates', []) if row['state'] in ('pending', 'retrying', 'inflight', 'checking') else []
                merged = {(u['Path'], u['UpdateType']): u for u in old_updates + fresh_updates}
                payload['updates'] = list(merged.values())
                payload.pop('coverage_checked', None)
                self.save(jid, state='pending', payload=json.dumps(payload, ensure_ascii=False),
                          attempts=0, next_due=now, deadline=now+self.DEADLINE_SECONDS,
                          message='新增路径已加入通知，保留此前入库核查进度。')
            # A separate persistent delivery loop performs the network request.
            # The organizer only records the intent and can continue immediately.
            self.engine.update_job(jid, notify_state='retrying')

    async def deliver(self, jid, now):
        row = self.row(jid)
        payload = json.loads(row['payload'])
        connection = self.connection()
        if not connection:
            self.save(jid, state='retrying', next_due=now+self.CHECK_SECONDS, message='等待配置 Emby，稍后自动重试。')
            self.engine.update_job(jid, notify_state='retrying')
            return
        url, key = connection
        fingerprint = digest('|'.join(connection))
        if payload['connection'] and payload['connection'] != fingerprint:
            return await self.finish(jid, 'unconfirmed', 'Emby 配置已改变，请检查新服务并手动扫描。')
        payload['connection'] = fingerprint
        updates=payload.setdefault('updates',self.updates(jid,payload))
        if updates:
            self.store.event('emby', f'📡 任务 {jid}：Emby 入库刷新：{len(updates)} 条路径变更')
        if not updates:
            self.save(jid,state='scan_only',message='本批没有可通知的路径变更，未发起 Emby 扫描。')
            self.engine.update_job(jid,notify_state='not_needed')
            self.store.event('emby',f'任务 {jid}：无路径变更，未请求全库扫描。')
            return
        attempts = row['attempts'] + 1
        self.save(jid, state='inflight', attempts=attempts, payload=json.dumps(payload, ensure_ascii=False), next_due=now+self.CHECK_SECONDS)
        self.engine.update_job(jid, notify_state='retrying')
        # → Library/Media/Updated。前两步只读探路，失败只记事件，绝不拦后面那条写。
        try:
            info = await emby_request(self.http, 'GET', url + '/System/Info/Public', timeout=10)
            if info.status_code == 200:
                public = info.json()
                self.store.event('emby', '任务 %s：Emby 服务 %s（%s）。'
                                 % (jid, public.get('ServerName') or '?', public.get('Version') or '?'))
        except (httpx.HTTPError, ValueError):
            self.store.event('emby', f'任务 {jid}：Emby 服务信息读取失败，继续发路径更新。')
        await self.library_folders(url, key)
        try:
            for start in range(0,len(updates),200):
                response=await emby_request(self.http,'POST',url+'/Library/Media/Updated',headers={'X-Emby-Token':key},json={'Updates':updates[start:start+200]},timeout=15)
                if response.status_code in (400,404,405):
                    return await self.finish(jid,'unconfirmed',f'Emby 路径通知返回 HTTP {response.status_code}，请检查版本与路径映射；未回退全库扫描。')
                response.raise_for_status()
        except httpx.HTTPError as exc:
            status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else type(exc).__name__
            self.store.event('emby',f'任务 {jid}：路径通知第 {attempts}/3 次未确认（{status}），未请求全库扫描。')
            if attempts >= self.MAX_ATTEMPTS:
                self.save(jid, state='checking', next_due=now+self.CHECK_SECONDS,
                          message='扫描通知已尝试 3 次，继续核对入库结果，不再补发。')
                self.engine.update_job(jid, notify_state='checking')
                return
            # 0.8.22：重试间隔加 ±20% 抖动，避免同一批任务在同一秒一起重发。
            self.save(jid, state='retrying',
                      next_due=now+self.CHECK_SECONDS*attempts*random.uniform(0.8, 1.2),
                      message=f'扫描通知暂未确认，将自动重试（已尝试 {attempts}/3 次）。')
            self.engine.update_job(jid, notify_state='retrying')
            return
        payload['sent_at']=time.time();payload['mode']='paths'
        self.engine.update_job(jid, notify_state='accepted')
        message=f'Emby 已接受 {len(updates)} 条路径变更；'+('后台核对本批 STRM，首次 3 秒后检查。' if payload['targets'] else '本批仅更新删除路径，不发起全库扫描；尚未核实 Emby 删除结果。')
        self.save(jid, state='accepted' if payload['targets'] else 'scan_only', payload=json.dumps(payload,ensure_ascii=False),next_due=now+self.FIRST_CHECK_SECONDS,message=message)
        self.engine.audit(jid, 0, message)
        self.store.event('emby',f'任务 {jid}：POST /Library/Media/Updated 接受新增/变更 {sum(u["UpdateType"]!="Deleted" for u in updates)}、删除 {sum(u["UpdateType"]=="Deleted" for u in updates)} 条路径，未请求全库扫描；核对超时后只刷新命中的目标库。')
        self.store.event('emby',f'任务 {jid}：Emby 侧路径示例：'+ '；'.join(u['Path'] for u in updates[:3])+('；其余路径已合并提交。' if len(updates)>3 else ''))

    @staticmethod
    def play_ids(item):
        """一条 Emby 条目里出现的全部播放入口 ID（`/play/<id>` 与 `/play/share/<sid>`）。

        0.8.21：从 `matches()` 里抽出来。`matches()` 原本对每个目标都重新解析一遍
        条目路径，而媒体信息补扫是「每个本地 STRM × 全部 Emby 条目」，等于把同一份
        解析做了几十万次——现场一次补扫把事件循环堵了 16 秒（看门狗抓到的栈：
        loop → sweep_missing → matches）。抽出后可以先建索引，再按 pid 查。
        """
        ids = set()
        paths = [item.get('Path')] + [source.get('Path') for source in item.get('MediaSources', [])
                                      if isinstance(source, dict)]
        for path in paths:
            if not isinstance(path, str):
                continue
            parsed = urlsplit(path)
            if parsed.scheme not in ('http', 'https'):
                continue
            # 分享库走 /play115/share/<sid>/…，不走网盘的 /play/{id}，parse_play_path 认不出来。
            share_match = re.fullmatch(r'/play115/share/([A-Za-z0-9_-]+)(?:/[^/]+)?', parsed.path)
            if share_match:
                ids.add('share/' + share_match[1])
                continue
            item_id, _ = parse_play_path(parsed.path)
            if item_id:
                ids.add(item_id)
        return ids

    @classmethod
    def matches(cls, item, target):
        paths = [item.get('Path')] + [source.get('Path') for source in item.get('MediaSources', [])
                                      if isinstance(source, dict)]
        # Emby 核对按播放身份判断（B02）：只要条目带有播放入口地址，
        # 就只认入口 ID 是否一致；旧格式 /play/{id} 与可读格式
        # /play/{id}/{slug} 等价，绝不因为文件路径相同就放行另一入口。
        playback_items = cls.play_ids(item)
        if playback_items:
            return target['pid'] in playback_items
        expected = target['relative'].replace('\\', '/').strip('/')
        for path in paths:
            if not isinstance(path, str):
                continue
            normalized = path.replace('\\', '/')
            if normalized == expected or normalized.endswith('/' + expected):
                return True
        return False

    async def items(self, params, headers, url, endpoint='/Items'):
        result = []
        for start in range(0, 100000, 200):
            query = {**params, 'StartIndex':start, 'Limit':200, 'Fields':'Path,ProviderIds,MediaSources'}
            if endpoint == '/Items': query['Recursive'] = 'true'
            response = await emby_request(self.http,'GET',url+endpoint, headers=headers,
                params=query, timeout=15)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get('Items'), list): raise ValueError('invalid items')
            page = data['Items']
            if not all(isinstance(item, dict) for item in page): raise ValueError('invalid item')
            result.extend(page)
            total = data.get('TotalRecordCount')
            if isinstance(total, int) and len(result) >= total:
                return result
            if len(page) < 200:
                if isinstance(total, int) and len(result) < total:
                    raise ValueError('Emby 枚举被截断，保留未知状态')
                return result
        raise ValueError('Emby 条目超过本次枚举上限，未发布不完整结果')

    async def verify(self, jid):
        row = self.row(jid); payload = json.loads(row['payload'])
        connection = self.connection()
        if not connection or digest('|'.join(connection)) != payload['connection']:
            return None
        url, key = connection; headers = {'X-Emby-Token':key}; groups = {}
        for target in payload['targets']:
            if target['confirmed']: continue
            group = (target['kind'], target['tmdb'], target['title'])
            groups.setdefault(group, []).append(target)
        # Query each work once, not the entire library or once per episode.
        pending = list(groups.items())
        cursor = payload.get('cursor', 0) % max(1, len(pending))
        pending = pending[cursor:] + pending[:cursor]
        duplicated = list(payload.get('duplicated_titles', []))
        for offset, ((kind, tmdb, title), targets) in enumerate(pending[:5]):
            payload['cursor'] = cursor + offset + 1
            self.save(jid, payload=json.dumps(payload, ensure_ascii=False))
            params = {'IncludeItemTypes':'Movie' if kind == 'movie' else 'Series' if kind == 'tv' else 'Movie,Episode'}
            if kind in ('movie', 'tv') and tmdb: params['AnyProviderIdEquals'] = f'tmdb.{tmdb}'
            else: params['SearchTerm'] = title
            candidates = await self.items(params, headers, url)
            # 同一个 TMDB 身份出现多个条目，说明同一作品被两套 STRM 产线各入库一次。
            if tmdb and len(candidates) > 1 and title not in duplicated:
                duplicated.append(title)
            if kind == 'tv':
                episodes = []
                for series in candidates:
                    sid = str(series.get('Id') or '')
                    if not re.fullmatch(r'[A-Za-z0-9_-]+', sid):
                        raise ValueError('Emby 剧集作品身份无效，保留未确认状态')
                    # /Items?ParentId=<Series> can return no episodes even
                    # when they exist under Season children. The show-specific
                    # endpoint enumerates across seasons and supports paging.
                    episodes.extend(await self.items({}, headers, url, '/Shows/' + sid + '/Episodes'))
                candidates = episodes
            for target in targets:
                matched = [item for item in candidates if self.matches(item, target)]
                # 同一条 Emby 剧集可能从不同系列父节点被重复返回；按条目 ID 去重，
                # 但不同 ID 仍视为歧义，不能对不确定的版本发起探测。
                unique, anonymous = {}, []
                for item in matched:
                    item_id = str(item.get('Id') or '')
                    if item_id: unique.setdefault(item_id, item)
                    else: anonymous.append(item)
                matched = list(unique.values()) + anonymous
                target['confirmed'] = bool(matched)
                # Only enqueue a uniquely identified item. Ambiguous paths must
                # never cause a probe on an unrelated Emby record.
                if len(matched) == 1:
                    # 是否真的去 ffprobe 由 native_media_info.tick 按「同步设置 → 媒体信息提取」
                    # 决定；提取成功写完 sidecar 会立刻写进 Emby（见 write_sidecar 末尾）。
                    self.media_info.enqueue(jid, matched[0], target, payload['connection'])
            self.save(jid, payload=json.dumps(payload, ensure_ascii=False))
            await asyncio.sleep(0)
        payload['duplicated_titles'] = duplicated[:20]
        self.save(jid, payload=json.dumps(payload, ensure_ascii=False))
        targets = payload['targets']
        return {'total': len(targets), 'confirmed': sum(1 for t in targets if t['confirmed']),
                'duplicates': duplicated[:5]}

    async def finish(self, jid, state, message, progress=None):
        payload=json.loads(self.row(jid)['payload'])
        if progress:
            payload['progress']={'total':progress['total'],'confirmed':progress['confirmed']}
            if progress['total'] and state != 'verified':
                message += f' 已确认入库 {progress["confirmed"]}/{progress["total"]}。'
            if progress.get('duplicates'):
                message += (' 同一作品在 Emby 有多个条目（' + '、'.join(progress['duplicates'][:3]) +
                            '），可能由另一套 STRM 产线重复入库。')
        if state=='verified':
            payload['verified_at']=time.time()
            elapsed=max(0,round(payload['verified_at']-payload.get('sent_at',payload.get('ready_at',payload['verified_at']))))
            message+=f' 通知至本次核对确认约 {elapsed} 秒（不是 Emby 精确处理耗时）。'
        self.save(jid,payload=json.dumps(payload,ensure_ascii=False))
        self.save(jid, state=state, message=message)
        self.engine.update_job(jid, notify_state=state)
        self.store.event('emby', f'任务 {jid}：{message}')
        # 海报通知等待本次核对结果，避免同一批作品先报整理、再报入库。
        notification_config=self.store.get('notification_config',{})
        if (notification_config.get('telegram_enabled') and notification_config.get('categories',{}).get('media',True)
                and self.store.db.execute("SELECT 1 FROM media_notice_groups g LEFT JOIN notifications n ON n.event_key=g.event_key WHERE g.job_id=? AND (n.event_key IS NULL OR n.telegram_state NOT IN ('failed','disabled')) LIMIT 1",(jid,)).fetchone()):
            return
        task = asyncio.create_task(self.engine.notifications.publish(
            f'emby-result:{jid}:{state}:{len(payload.get("handled_items", []))}:{(progress or {}).get("total", 0)}:{(progress or {}).get("confirmed", 0)}',
            f'Emby 入库检查 #{jid}', message, 'success' if state == 'verified' else 'warning', jid))
        self.engine.notice_tasks.add(task)
        task.add_done_callback(lambda done: (self.engine.notice_tasks.discard(done), None if done.cancelled() else done.exception()))

    @staticmethod
    def deadline_state(progress):
        """到点时按实际完成度给结论，而不是一律「未确认」。"""
        if progress and progress['total']:
            total, confirmed = progress['total'], progress['confirmed']
            if confirmed and confirmed < total:
                return ('partial',
                        f'{confirmed}/{total} 条 STRM 已入库，{total - confirmed} 条在 30 分钟内未确认；'
                        '请在 Emby 核对这些路径的媒体库与扫描结果。', progress)
        return 'unconfirmed', '30 分钟内未确认本批 STRM 入库，请检查 Emby 挂载、媒体库与连接。', progress

    async def tick(self, now=None):
        now = time.time() if now is None else now
        async with self.gate:
            row = self.store.db.execute("SELECT * FROM emby_deliveries WHERE state IN ('pending','retrying','accepted','inflight','checking') AND next_due<=? ORDER BY next_due,job_id LIMIT 1", (now,)).fetchone()
            if not row: return
            jid = row['job_id']
            connection = self.connection()
            fingerprint = json.loads(row['payload']).get('connection')
            if connection and fingerprint and digest('|'.join(connection)) != fingerprint:
                return await self.finish(jid, 'unconfirmed', 'Emby 配置已改变，请检查新服务并手动扫描。')
            progress = None
            if row['attempts']:
                try:
                    async with asyncio.timeout(20):
                        progress = await self.verify(jid)
                except (httpx.HTTPError, ValueError, TypeError, TimeoutError):
                    progress = None
                if progress and progress['total'] and progress['confirmed'] == progress['total']:
                    return await self.finish(jid, 'verified', '本批 STRM 已在 Emby 中找到；实际播放仍以播放器测试为准。', progress)
                if progress and progress['total'] and progress['confirmed'] < progress['total']:
                    current = json.loads(self.row(jid)['payload'])
                    if not current.get('coverage_checked'):
                        current['coverage_checked'] = True
                        self.save(jid, payload=json.dumps(current, ensure_ascii=False))
                        # 只在这里读一次媒体库根路径：正常入库的批次不会多出请求。
                        problem = await self.coverage_problem(jid, current)
                        if problem:
                            return await self.finish(jid, 'unconfirmed', problem, progress)
                    # 路径通知和 Emby 自带监控都没能在窗口内收录时，补一次
                    # 目标库刷新；结论仍取决于下面的逐条核对。
                    await self.maybe_scan(jid, json.loads(self.row(jid)['payload']), now)
            if now >= row['deadline']:
                return await self.finish(jid, *self.deadline_state(progress))
            if row['state'] in ('pending','retrying','inflight'):
                if row['attempts'] >= self.MAX_ATTEMPTS:
                    return await self.finish(jid, 'unconfirmed', 'Emby 扫描通知未确认；自动重试已达上限，请检查连接。', progress)
                await self.deliver(jid, now)
            else:
                payload=json.loads(self.row(jid)['payload'])
                interval=self.CHECK_SECONDS if now-payload.get('sent_at',now)<120 else 30
                message='Emby 已接受路径通知，部分 STRM 尚未在媒体库中找到；尚未请求库刷新。'
                if progress and progress['total']:
                    message=f'Emby 已接受路径通知，已确认入库 {progress["confirmed"]}/{progress["total"]}；尚未请求库刷新。'
                scans=payload.get('scans') or []
                if scans:
                    head=(f'已确认入库 {progress["confirmed"]}/{progress["total"]}' if progress and progress['total']
                          else '本批 STRM 尚未全部找到')
                    message=f'Emby 已接受路径通知，{head}；已请求刷新媒体库「{scans[-1]["library"]}」，继续逐条核对。'
                self.save(jid, next_due=now+interval, message=message)

    async def loop(self):
        while not self.engine.closing:
            try:
                await self.drain_outbox()
                await self.tick()
            except asyncio.CancelledError: raise
            except Exception:
                self.store.event('emby', '入库检查暂时失败，将在下次周期继续。')
                await asyncio.sleep(10)
            await asyncio.sleep(1)


class EmbyPrewarm:
    """只读预热：按用户拉一遍「继续观看」「最新」，再拉库统计。

    对齐  的每小时 `Emby 后台预热完成 users=.. continue_updated=.. library_ok=..`。
    目的是让 Emby 首页、继续观看、库计数常驻缓存，首次打开不再等它自己算。
    失败只记事件，不改任何任务状态，也不让调度被判为故障。
    """

    DEFAULT_MINUTES = 60
    RESUME_LIMIT = 12
    LATEST_LIMIT = 24

    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self._last = 0.0

    def minutes(self):
        raw = self.store.get('emby_prewarm_minutes')
        return self.DEFAULT_MINUTES if raw in (None, '') else int(raw)

    async def tick(self, now=None):
        now = time.time() if now is None else now
        minutes = self.minutes()
        if minutes <= 0 or now - self._last < minutes * 60:
            return None
        connection = self.engine.emby.connection()
        if not connection:
            return None
        url, key = connection
        self._last = now
        headers = {'X-Emby-Token': key}
        counts = {'users': 0, 'resume': 0, 'latest': 0, 'libraries': 0, 'failed': 0}
        try:
            response = await emby_request(self.engine.http, 'GET', url + '/Users',
                                          headers=headers, timeout=15)
            response.raise_for_status()
            users = [row.get('Id') for row in response.json() or [] if row.get('Id')]
            counts['users'] = len(users)
            for user_id in users:
                for path, params, field in (
                        ('/Users/%s/Items/Resume' % user_id,
                         {'Limit': self.RESUME_LIMIT, 'MediaTypes': 'Video'}, 'resume'),
                        ('/Users/%s/Items' % user_id,
                         {'SortBy': 'DateCreated', 'SortOrder': 'Descending', 'Recursive': 'true',
                          'IncludeItemTypes': 'Movie,Episode', 'Limit': self.LATEST_LIMIT}, 'latest')):
                    try:
                        item_response = await emby_request(self.engine.http, 'GET', url + path,
                                                           headers=headers, params=params, timeout=15)
                        item_response.raise_for_status()
                        counts[field] += len((item_response.json() or {}).get('Items') or [])
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        counts['failed'] += 1
            try:
                stats = await emby_request(self.engine.http, 'GET', url + '/Items/Counts',
                                           headers=headers, timeout=15)
                stats.raise_for_status()
                counts['libraries'] = len(stats.json() or {})
            except asyncio.CancelledError:
                raise
            except Exception:
                counts['failed'] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            counts['failed'] += 1
        self.store.event('emby', 'Emby 后台预热完成 users=%d resume=%d latest=%d libraries=%d failed=%d。'
                         % (counts['users'], counts['resume'], counts['latest'],
                            counts['libraries'], counts['failed']))
        return counts
