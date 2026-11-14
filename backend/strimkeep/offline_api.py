"""离线下载（磁力 / 电驴 / 直链）提交、任务列表与完成后的批次整理（U06）。

提交登记先行：先落 before_ids 快照与意图，再请求 115；响应丢失不自动
重复提交，只转 needs_review 由任务列表/目录核查。完成后定位实际产物，
只整理本批内容（only_file_ids + source_entries），并禁用目录清理。
"""
import json
import re
import time

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .cloud115 import CloudError
from .message_templates import offline_completed, offline_stalled

ALLOWED_PREFIXES = ('magnet:', 'ed2k://', 'http://', 'https://', 'ftp://')
OFFLINE_MAX_LINKS = 100          # 单次提交链接上限（Web 与 Telegram 共用）


def ensure_link_count(urls):
    """链接数量闸门：超过上限直接 422，不做截断（避免静默丢链接）。"""
    if len(urls) > OFFLINE_MAX_LINKS:
        raise HTTPException(422, f'单次最多提交 {OFFLINE_MAX_LINKS} 个链接。')

_MAGNET_HASH = re.compile(r'(?i)btih:([A-Za-z0-9]+)')
# ed2k 链接形如 ed2k://|file|名称|大小|32位哈希|/，云端任务的 info_hash 与它一致。
_ED2K_HASH = re.compile(r'(?i)ed2k://\|file\|[^|]*\|\d+\|([0-9a-f]{32})\|')


class OfflineRoute(BaseModel):
    """关键词二次分流：帖子/文本命中关键词就落到这个目录。"""
    model_config = ConfigDict(extra='forbid')
    keywords: str = Field(default='', max_length=200)
    target_cid: str = Field(default='', max_length=24)
    target_label: str = Field(default='', max_length=500)


class OfflineConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_cid: str = Field(default='', max_length=24)
    target_label: str = Field(default='', max_length=500)
    magnet_cid: str = Field(default='', max_length=24)
    magnet_label: str = Field(default='', max_length=500)
    # ED2K 独立目录（以前和直链共用 target_cid）
    ed2k_cid: str = Field(default='', max_length=24)
    ed2k_label: str = Field(default='', max_length=500)
    bot_magnet_cid: str | None = Field(default=None, max_length=24)
    bot_magnet_label: str | None = Field(default=None, max_length=500)
    bot_ed2k_cid: str | None = Field(default=None, max_length=24)
    bot_ed2k_label: str | None = Field(default=None, max_length=500)
    directories_v2: bool = False
    magnet_routes: list[OfflineRoute] = Field(default_factory=list, max_length=20)
    ed2k_routes: list[OfflineRoute] = Field(default_factory=list, max_length=20)
    auto_organize: bool = False          # Web 提交离线：落盘后是否立即整理
    bot_auto_organize: bool = False      # Telegram 提交离线：同上（分开设置）
    magnet_auto_organize: bool | None = None      # Web 磁力/直链（None=跟随 auto_organize）
    ed2k_auto_organize: bool | None = None        # Web ED2K（None=跟随 auto_organize）
    bot_magnet_auto_organize: bool | None = None  # 机器人磁力/直链（None=跟随 bot_auto_organize）
    bot_ed2k_auto_organize: bool | None = None    # 机器人 ED2K（None=跟随 bot_auto_organize）
    # 仅为兼容旧版配置/客户端而保留；离线目录兜底扫描已停用。
    scan_minutes: int = Field(default=5, ge=0, le=1440)


def route_target(routes, text, cid, label):
    """按关键词挑目录：命中第一条就用它的目录，都没命中用默认目录。"""
    lowered = str(text or '').casefold()
    for route in routes or []:
        get = route.get if isinstance(route, dict) else lambda k, default='': getattr(route, k, default)
        words = [w.strip().casefold() for w in str(get('keywords', '') or '').split(',') if w.strip()]
        if words and any(w in lowered for w in words):
            target = str(get('target_cid', '') or '')
            if target:
                return target, (str(get('target_label', '') or '') or target)
    return cid, label


def directory_config(raw):
    """Copy legacy effective destinations into independent values on first save."""
    raw = raw or {}
    value = {**OfflineConfig().model_dump(), **raw}
    if not value.get('directories_v2'):
        if not value.get('ed2k_cid'):
            value['ed2k_cid'], value['ed2k_label'] = value.get('target_cid', ''), value.get('target_label', '')
        for kind, old in (('magnet', 'magnet'), ('ed2k', 'target')):
            if value.get('bot_'+kind+'_cid') is None:
                value['bot_'+kind+'_cid'] = value.get(old+'_cid', '')
                value['bot_'+kind+'_label'] = value.get(old+'_label', '')
    for new, old in (('magnet_auto_organize', 'auto_organize'), ('ed2k_auto_organize', 'auto_organize'),
                     ('bot_magnet_auto_organize', 'bot_auto_organize'), ('bot_ed2k_auto_organize', 'bot_auto_organize')):
        if value.get(new) is None:
            value[new] = bool(value.get(old, False))
    return value


def organize_flag_for(cfg, kind, origin):
    """按链接类型与来源取「落盘后立即整理」开关；直链跟随磁力，UI 三列对齐保存目录。"""
    if origin == 'bot':
        key = 'bot_ed2k_auto_organize' if kind == 'ed2k' else 'bot_magnet_auto_organize'
    else:
        key = 'ed2k_auto_organize' if kind == 'ed2k' else 'magnet_auto_organize'
    return bool(cfg.get(key, False))


class OfflineInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=16000)


def _split(text):
    """一行一个链接；也容忍逗号或空格分隔的粘贴。"""
    out = []
    for line in text.replace('\r', '\n').split('\n'):
        for piece in line.replace(',', ' ').split():
            if piece:
                out.append(piece.strip())
    return out


def links_in(text):
    """挑出可提交的链接，保持顺序并去重。Web 与 Telegram 机器人共用。"""
    out = []
    for url in _split(text):
        if url.lower().startswith(ALLOWED_PREFIXES) and url not in out:
            out.append(url)
    return out


def _tasks(rows):
    """115 返回结构在不同接口版本里有 data / tasks 两种外层，统一成列表。"""
    data = rows if isinstance(rows, list) else None
    if data is None and isinstance(rows, dict):
        data = rows.get('data')
        if isinstance(data, dict):
            data = data.get('tasks') or data.get('data')
        if data is None:
            data = rows.get('tasks')
    if not isinstance(data, list):
        data = []
    items = []
    for task in data:
        if not isinstance(task, dict):
            continue
        items.append({
            'id': str(task.get('info_hash') or task.get('file_id') or task.get('id') or ''),
            # 115 的 file_id 是「保存到的目录」，产物文件的 ID 在 delete_file_id。
            # 现场就是因为把目录 ID 当产物 ID，所有离线任务都认领不到产物。
            'file_id': str(task.get('delete_file_id') or task.get('fid') or ''),
            'dir_id': str(task.get('file_id') or task.get('wp_path_id') or ''),
            'name': task.get('name') or task.get('file_name') or task.get('title') or '',
            'percent': task.get('percentDone') or task.get('percent_done') or task.get('percent') or 0,
            'status': task.get('status_text') or task.get('statusText') or task.get('status') or '',
            'display': str(task.get('display_status') or ''),
            'size': task.get('size') or task.get('file_size') or 0,
            'url': task.get('url') or '',
        })
    return items, len(data)


def _magnet_key(url):
    match = _MAGNET_HASH.search(url or '')
    return match[1].lower() if match else None


def _link_key(url):
    """认领键：磁力/电驴用 hash（云端会改写链接），其余退回 URL 包含匹配。"""
    magnet = _magnet_key(url)
    if magnet:
        return 'hash', magnet
    ed2k = _ED2K_HASH.search(url or '')
    if ed2k:
        return 'hash', ed2k.group(1).lower()
    return 'url', url or ''


class OfflinePostProcess:
    """离线提交登记与完成核查（U06）：提交 → 云端任务 → 产物 → 本批整理任务。"""

    INTERVAL = 60
    EXPIRE_SECONDS = 86400

    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.next_run = 0.0
        # offline_submissions 也由 store 启动 schema 建好；这里保留幂等 DDL（该类会被
        # 单独实例化），只在老库缺列时做补列迁移。
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS offline_submissions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, urls TEXT NOT NULL,
            target_cid TEXT NOT NULL, target_label TEXT NOT NULL DEFAULT '',
            before_ids TEXT NOT NULL DEFAULT '[]', submitted_at REAL NOT NULL,
            last_checked REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'submitted',
            message TEXT NOT NULL DEFAULT '', organize_job_id INTEGER,
            organize INTEGER NOT NULL DEFAULT 1)''')
        cols = {row[1] for row in self.store.db.execute('PRAGMA table_info(offline_submissions)')}
        if 'organize' not in cols:
            self.store.db.execute('ALTER TABLE offline_submissions ADD COLUMN organize INTEGER NOT NULL DEFAULT 1')
        if 'finished_seen_at' not in cols:
            self.store.db.execute('ALTER TABLE offline_submissions ADD COLUMN finished_seen_at REAL NOT NULL DEFAULT 0')
        self.store.db.commit()

    async def submit(self, urls, target_cid, target_label, organize=True, origin='web'):
        """先登记意图与目录快照，再提交 115；失败不重复提交，转 needs_review。

        `organize=False` 表示只落盘：下载完成后不自动入队整理，等定时/手动扫描接手。
        频道监控固定走这条，避免「落盘即整理」；手动离线按设置里的开关决定。
        """
        if not target_cid or not re.fullmatch(r'[1-9]\d{0,23}', target_cid):
            raise CloudError('请先配置有效的离线下载目录。', 'OFFLINE_NO_TARGET', 409)
        # 同一个链接已经在处理中就直接拒绝，和转存一样先查再提交，别让 115 那边报「任务已存在」。
        wanted = {_link_key(url) for url in urls}
        for old in self.store.db.execute(
                "SELECT id,urls FROM offline_submissions"
                " WHERE state IN ('submitting','submitted','needs_review')"):
            try:
                existing = {_link_key(url) for url in json.loads(old['urls'] or '[]')}
            except (TypeError, ValueError):
                continue
            if wanted & existing:
                raise CloudError('该链接已在离线任务中（#%d），不会重复提交。' % old['id'],
                                 'OFFLINE_DUPLICATE', 409)
        before = [e['id'] for e in await self.engine.cloud.children(target_cid)]
        with self.store.db:
            sid = self.store.db.execute(
                'INSERT INTO offline_submissions(urls,target_cid,target_label,before_ids,submitted_at,state,message,organize) '
                "VALUES(?,?,?,?,?, 'submitting','',?)",
                (json.dumps(urls, ensure_ascii=False), target_cid, target_label,
                 json.dumps(before), time.time(), 1 if organize else 0)).lastrowid
        try:
            await self.engine.cloud.offline_add(urls, target_cid)
        except Exception as exc:
            text = str(exc) or type(exc).__name__
            if '任务已存在' in text:
                # 115 明确说任务已存在：链接早就提交过了，转核查即可，不是失败。
                # 现场 28 条重复提交被写成 needs_review，永久挂在待人工。
                self._set(sid, 'submitted', '115 提示任务已存在（重复提交），转核查下载产物。')
                self.store.event('offline', '离线提交 #%d（%s）重复：%s；转核查。'
                                 % (sid, origin, text[:60]))
                return sid
            # 任何提交失败（含响应丢失/超时）都只登记待核查，绝不自动重复提交（U06）。
            self._set(sid, 'needs_review',
                      '提交响应未成功返回（' + (str(exc)[:80] or type(exc).__name__) +
                      '）；请到 115 离线任务列表核查结果，本条不会自动重复提交。')
            # 失败也要进实时日志：机器人只回一句「待核查」，事后没有痕迹就无法对账。
            self.store.event('offline', '离线提交 #%d（%s）响应未确认：%s；请到 115 离线任务列表核查，本条不会自动重复提交。'
                             % (sid, origin, str(exc)[:80] or type(exc).__name__))
            raise
        self._set(sid, 'submitted',
                  '已提交，等待下载完成' + ('后定位本批产物并自动整理。' if organize else '；只落盘，不自动整理。'))
        # 实时日志与来源无关：Web 与 Telegram 机器人提交都要留痕（#%d 便于和离线列表对账）。
        self.store.event('offline', '离线提交 #%d（%s）：%d 个链接 → %s；等待下载完成%s。'
                         % (sid, origin, len(urls), target_label or target_cid,
                            '后自动整理' if organize else '（只落盘）'))
        return sid

    RETRY_LIMIT = 6

    def _retry_later(self, row, message):
        """核查失败按次数退避：115 超时/限流是临时的，不能一次就判死。

        现场 99 条「核查未完成：115 请求未完成」全部一次就转 needs_review，再没人碰。
        保持 submitted 状态让它下一轮继续核查，连续 RETRY_LIMIT 次才转人工。
        """
        attempts = int(row['attempts'] or 0) + 1
        state = 'needs_review' if attempts >= self.RETRY_LIMIT else 'submitted'
        note = ('%s（已重试 %d 次，转人工）' % (message, attempts) if state == 'needs_review'
                else '%s（第 %d 次，稍后重试）' % (message, attempts))
        with self.store.db:
            self.store.db.execute(
                'UPDATE offline_submissions SET state=?,message=?,attempts=?,last_checked=? WHERE id=?',
                (state, note, attempts, time.time(), row['id']))

    # 现场口径：正常离线 5 分钟、最多 10 分钟就下完；超过这个时间基本就是下不动了。
    STALL_SECONDS = 10 * 60

    @staticmethod
    def _duration(seconds):
        seconds = max(0, int(seconds))
        if seconds >= 86400:
            return '%d 天' % (seconds // 86400)
        if seconds >= 3600:
            return '%d 小时' % (seconds // 3600)
        return '%d 分钟' % max(1, seconds // 60)

    @staticmethod
    def _url_names(raw):
        """从提交的链接里取一个能认人的名字（ed2k 的文件名 / 磁力的 dn 参数）。"""
        try:
            urls = json.loads(raw or '[]')
        except (TypeError, ValueError):
            return ''
        for url in urls or []:
            text = str(url)
            m = re.search(r'\|file\|([^|]+)\|', text)
            if m:
                return m.group(1)
            if text.startswith('magnet:'):
                m = re.search(r'[?&]dn=([^&]+)', text)
                if m:
                    return m.group(1).replace('+', ' ')
        return ''

    async def report_stalled(self, now=None, ids=None):
        """卡在下载中的离线任务，每 12 小时汇总回执一次。

        现场痛点：离线任务一直不完成时系统既不报错也不推进，用户完全不知道是卡住了
        还是在慢慢下，也就没法去 115 换源。这里给一条能直接动手的回执。
        """
        now = time.time() if now is None else now
        cutoff = now - self.STALL_SECONDS
        # 必须同时满足：本轮刚核查过（ids）、115 明确回「下载进行中」、还没回执过。
        # 少任何一条都不能判超时——刚提交还没轮到核查、或者任务已被删，都不是超时。
        scope = " AND id IN (%s)" % ','.join('?' * len(ids)) if ids else ''
        rows = self.store.db.execute(
            "SELECT id,urls,target_cid,target_label,submitted_at FROM offline_submissions"
            " WHERE state='submitted' AND submitted_at<? AND stalled_at IS NULL"
            " AND message LIKE '%下载进行中%'" + scope +
            " ORDER BY submitted_at LIMIT 5",
            (cutoff, *(ids or []))).fetchall()
        if not rows:
            return 0
        sent = 0
        for row in rows:
            try:
                urls = json.loads(row['urls'] or '[]')
            except (TypeError, ValueError):
                urls = []
            name = self._url_names(row['urls'])
            heading, body = offline_stalled(self._label(row['target_cid'], row['target_label']),
                                            len(urls or []), name)
            await self.engine.notifications.publish(
                key='offline_stalled:%d' % row['id'], title=heading, message=body,
                level='info', category='offline')
            with self.store.db:
                self.store.db.execute('UPDATE offline_submissions SET stalled_at=? WHERE id=?',
                                      (now, row['id']))
            self.store.event('offline', '离线提交 #%d 超过 10 分钟没下完，已发回执。' % row['id'])
            sent += 1
        return sent

        return total

    def _label(self, cid, fallback=''):
        """目录名优先取当前配置：目录改了名，回执跟着变；没有配置才用提交时的快照。"""
        return self.store.directory_label(cid, fallback)

    def _set(self, sid, state, message):
        with self.store.db:
            self.store.db.execute('UPDATE offline_submissions SET state=?,message=?,last_checked=? WHERE id=?',
                                  (state, message, time.time(), sid))

    EXPIRE_SECONDS = 3 * 86400
    # 115 说完成到产物出现在目录之间可能有个延迟，给 5 分钟宽限再判「产物不在」。
    PRODUCT_GRACE_SECONDS = 300

    async def tick(self, now=None):
        now = time.time() if now is None else now
        if now < self.next_run:
            return
        self.next_run = now + self.INTERVAL
        # 超过 72 小时还没认领产物的：离线任务早就结束了，产物要么已落盘要么失败。
        # 继续核查、继续挂人工只是噪音（现场 166 条里 110 条属于这种）。
        with self.store.db:
            expired = self.store.db.execute(
                "UPDATE offline_submissions SET state='expired', last_checked=?,"
                " message='离线提交超过 72 小时未认领产物，自动结项。'"
                " WHERE state IN ('submitted','needs_review','submitting') AND submitted_at<?",
                (now, now - self.EXPIRE_SECONDS)).rowcount
        if expired:
            self.store.event('offline', '%d 条离线提交超过 72 小时未认领产物，已自动结项。' % expired)
        checked = []
        for row in self.store.db.execute(
                "SELECT * FROM offline_submissions WHERE state IN ('submitted','submitting') ORDER BY last_checked,id LIMIT 5").fetchall():
            try:
                await self.check(row)
            except CloudError as exc:
                if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
                    return  # 等登录恢复，不算失败。
                self._retry_later(row, '核查未完成：' + str(exc)[:120])
            except Exception as exc:
                self._retry_later(row, '核查未完成：' + type(exc).__name__)
            checked.append(row['id'])
        # 只报「本轮核查过、且 115 明确说还在下载」的：没核查过的不能猜，被删的也不算超时。
        try:
            await self.report_stalled(now, checked)
        except Exception:
            pass

    def _matching_tasks(self, urls, tasks):
        """把提交的链接与 115 离线任务对应：磁力/电驴按 hash，其余按 URL 包含。

        电驴以前只按 URL 包含匹配，而 115 会把链接规范化（大小写/名称），导致永远认领不到
        任务、最后落到 needs_review——表现为「手动提交的 ed2k 不自动整理」。
        """
        keys = [_link_key(url) for url in urls]
        matched = []
        for task in tasks:
            for kind, value in keys:
                if kind == 'url':
                    if value and task.get('url') and value in task['url']:
                        matched.append(task)
                        break
                elif value and (task.get('id') or '').lower() == value:
                    matched.append(task)
                    break
        return matched

    # 否定 / 中间态必须先于「完成」「成功」判：中文状态是子串匹配，'未完成'
    # 里含 '完成'、'未成功' 里含 '成功'，先判肯定词就会把还在下载的任务当成已完成，
    # check() 直接跳到产物认领，拿不完整的东西往下走。
    RUNNING_WORDS = ('未完成', '未成功', '未开始', '等待', '排队', '暂停', '进行中', '下载中')
    FAILED_WORDS = ('失败', '错误', '违规', '被封')
    DONE_WORDS = ('完成', '成功')

    @staticmethod
    def _state(task):
        """离线任务三态：done / failed / running。

        判定顺序：失败 → 明确的中间态 → 百分比 100 → 状态文字完成 → 有进度算下载中。

        失败必须先判：以前 `_done()` 对「失败」返回 False，check() 就把它当成
        「下载进行中」直接 return，失败分支永远走不到，任务会一直显示下载中。
        """
        status = str(task.get('status') or '')
        display = str(task.get('display') or '')
        if any(word in status for word in OfflinePostProcess.FAILED_WORDS) or display in ('failed', 'error'):
            return 'failed'
        if any(word in status for word in OfflinePostProcess.RUNNING_WORDS):
            return 'running'
        if display in ('downloading', 'pending', 'waiting', 'running'):
            return 'running'
        percent = task.get('percent')
        value = None
        if percent not in (None, ''):
            try:
                value = float(str(percent).strip().rstrip('%'))
            except (TypeError, ValueError):
                value = None
        if value is not None and value >= 100:
            return 'done'
        if display == 'finished' or any(word in status for word in OfflinePostProcess.DONE_WORDS):
            return 'done'
        if value is not None and value > 0:
            # 状态文字认不出来但已经有进度：进度本身就是「还在下载」的证据，
            # 不能报 unknown —— unknown 会让 check() 每轮都退回「稍后重试」。
            return 'running'
        # 认不出来的状态不能默认成「下载中」——那会让超时回执凭空开火。
        return 'unknown'

    @classmethod
    def _done(cls, task):
        return cls._state(task) == 'done'

    async def check(self, row):
        urls = json.loads(row['urls'])
        target = row['target_cid']
        # 查询在线任务状态（先查询结果，绝不重复提交）。
        tasks = []
        try:
            for page in (1, 2, 3):
                tasks_raw, _ = await self.engine.cloud.offline_tasks(page, 50)
                items, _total = _tasks(tasks_raw)
                tasks.extend(items)
                if len(items) < 50:
                    break
        except CloudError as exc:
            if exc.code not in ('FILE_NOT_FOUND', 'OBJECT_NOT_FOUND'):
                raise
        # 一条提交里的每个链接都必须唯一匹配到云端任务；目录有新增文件并不证明属于本次提交。
        matched = []
        for url in urls:
            matches = self._matching_tasks([url], tasks)
            if len(matches) != 1 or matches[0] in matched:
                age = time.time() - row['submitted_at']
                if age > self.EXPIRE_SECONDS:
                    self._set(row['id'], 'needs_review', '无法唯一匹配全部离线任务，请人工核查；不会认领目录中的其他文件。')
                else:
                    self._set(row['id'], 'submitted', '等待确认离线任务身份；不会认领目录中的其他文件。')
                return
            matched.append(matches[0])
        if not matched:
            self._set(row['id'], 'needs_review', '提交记录缺少有效链接，无法确认下载产物。')
            return
        states = [self._state(task) for task in matched]
        if any(state == 'failed' for state in states):
            self._set(row['id'], 'needs_review',
                      '部分离线任务失败（%d/%d），请到 115 离线列表核查；不会自动重新提交。'
                      % (states.count('failed'), len(states)))
            return
        if any(state == 'unknown' for state in states):
            self._set(row['id'], 'submitted', '115 未返回明确状态，稍后重试。')
            return
        if any(state != 'done' for state in states):
            if row['finished_seen_at']:
                with self.store.db:
                    self.store.db.execute('UPDATE offline_submissions SET finished_seen_at=0 WHERE id=?', (row['id'],))
            self._set(row['id'], 'submitted', '下载进行中（任务列表 %d/%d 已完成）。' % (
                states.count('done'), len(states)))
            return
        finished_seen_at = float(row['finished_seen_at'] or 0)
        if not finished_seen_at:
            finished_seen_at = time.time()
            with self.store.db:
                self.store.db.execute('UPDATE offline_submissions SET finished_seen_at=? WHERE id=? AND finished_seen_at=0', (finished_seen_at, row['id']))
        # 产物认领：按任务给的产物文件 ID 直接查文件本身。
        # 不能靠 target_cid 列目录——115 报的 wp_path_id 有时和真实落盘目录不一致
        # （现场 SSNI-953 实际落在 3503430221283985095，wp_path_id 却是 porn 目录）。
        produced = []
        for task in matched:
            fid = task.get('file_id') or ''
            if not re.fullmatch(r'[1-9]\d*', fid):
                produced = []
                break
            try:
                entry = await self.engine.cloud.file(fid)
            except CloudError as exc:
                if exc.code != 'FILE_NOT_FOUND':
                    raise
                produced = []
                break
            produced.append(entry)
        if not produced:
            age = time.time() - row['submitted_at']
            if age > self.EXPIRE_SECONDS:
                self._set(row['id'], 'needs_review', '无法唯一确认离线任务产物，请人工核查；不会认领其他文件。')
            elif time.time() - finished_seen_at > self.PRODUCT_GRACE_SECONDS:
                # 115 明确说下载完成：回执照发（产物不在就注明），不静默。
                # 现场 115 的任务列表里有 20 个 done，目标目录却一个都没多——这种情况很常见。
                self._set(row['id'], 'completed',
                          '115 显示下载完成，但产物不在目标目录（可能已被移动或删除）。')
                heading, body = offline_completed(
                    self._label(row['target_cid'], row['target_label']), None)
                await self.engine.notifications.publish(
                    key='offline_done:%d' % row['id'], title=heading, message=body,
                    level='info', category='offline')
                self.store.event('offline', '离线提交 #%d：115 显示完成（产物不在目录），已发回执。'
                                 % row['id'])
            else:
                self._set(row['id'], 'submitted', '等待通过文件 ID 确认本次下载产物。')
            return
        try:
            organize = row['organize']
        except (IndexError, KeyError):
            organize = 1
        if not organize or not await self.engine.scan_scope_allows([row['target_cid']]):
            # 只落盘；目标目录不在待整理范围时同样只落盘，不建整理任务。
            self._set(row['id'], 'completed', '下载完成，本批 %d 项已落盘。' % len(produced))
            heading, body = offline_completed(self._label(row['target_cid'], row['target_label']),
                                              len(produced))
            await self.engine.notifications.publish(
                key='offline_done:%d' % row['id'], title=heading, message=body,
                level='info', category='offline')
            return
        job_id = await self._enqueue_organize(row, produced)
        heading, body = offline_completed(self._label(row['target_cid'], row['target_label']),
                                          len(produced), job_id)
        await self.engine.notifications.publish(
            key='offline_done:%d' % row['id'], title=heading, message=body,
            level='info', category='offline')

    async def _enqueue_organize(self, row, produced):
        config = self.engine.tasks.config('organize')
        # 产物实际落在哪个目录就以哪个为准；拿不准才退回提交时的目标目录。
        dirs = {str(e.get('parent_id') or '') for e in produced} - {''}
        source = list(dirs) if len(dirs) == 1 else [row['target_cid']]
        config = {**config, 'source_cids': source,
                  'source_entries': produced,
                  'origin': 'offline:' + str(row['id']), 'notify_emby': True,
                  'cleanup_disabled': True, 'scheduled': False}
        # source_entries 已把扫描限定在已核实的顶层条目；目录内的视频 ID 与目录 ID 不同。
        if not any(e['is_dir'] for e in produced):
            config['only_file_ids'] = [e['id'] for e in produced]
        else:
            config.pop('only_file_ids', None)
        now = time.time()
        with self.store.db:
            def comparable(value):
                value = dict(value)
                value.pop('origin', None)
                if isinstance(value.get('source_entries'), list):
                    value['source_entries'] = sorted(value['source_entries'], key=lambda e: str(e['id']))
                if isinstance(value.get('only_file_ids'), list):
                    value['only_file_ids'] = sorted(value['only_file_ids'])
                return value
            jid = None
            for existing in self.store.db.execute("SELECT id,config FROM organize_jobs WHERE state='queued'"):
                try:
                    previous = json.loads(existing['config'])
                    if str(previous.get('origin', '')).startswith('offline:') and comparable(previous) == comparable(config):
                        jid = existing['id']
                        break
                except (TypeError, ValueError, KeyError):
                    continue
            merged = jid is not None
            if jid is None:
                jid = self.store.jobs.create(json.dumps(config, ensure_ascii=False))
            self.store.db.execute(
                'UPDATE offline_submissions SET state=?,message=?,organize_job_id=? WHERE id=?',
                ('completed', f'下载完成，本批 {len(produced)} 项已{ "合并到" if merged else "加入" }整理任务 #{jid}。', jid, row['id']))
        self.store.event('offline', f'离线提交 #{row["id"]} 完成，整理任务 {jid} 已{ "合并" if merged else "入队" }（仅本批 {len(produced)} 项）。')
        return jid


def install_offline(app, store, admin):
    def config_value():
        return directory_config(store.get('offline_config', {}))

    @app.post('/api/v1/offline/submissions/expire', dependencies=[Depends(admin)])
    async def expire_submissions(days: int = Query(7, ge=1, le=365)):
        """把长期未确认产物的离线提交标为放弃，避免列表永远挂着一堆待核查。"""
        cutoff = time.time() - days * 86400
        with store.db:
            cursor = store.db.execute(
                "UPDATE offline_submissions SET state='expired', message=? "
                "WHERE state='needs_review' AND submitted_at<?",
                ('超过 %d 天未能确认产物，已标为放弃；需要时请重新提交链接。' % days, cutoff))
            count = cursor.rowcount
        if count:
            store.event('offline', '已放弃 %d 条超过 %d 天未确认的离线提交。' % (count, days))
        return {'expired': count}

    class IntakeInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        text: str = Field(min_length=1, max_length=20000)

    @app.post('/api/v1/intake', dependencies=[Depends(admin)])
    async def intake(body: IntakeInput):
        """一个输入框：自动拆分 115 分享 / 磁力 / ED2K / 直链，各自落到对应目录。

        分享走转存（/transfer 的 enqueue），其余走离线（按类型分组，各自目录与关键词分流）。
        任一类型失败不影响其它类型，结果里逐项返回。
        """
        from .transfer import shares as parse_share_links
        result = {'share': [], 'offline': [], 'errors': []}
        text = body.text

        share_links = []
        try:
            share_links = parse_share_links(text)
        except ValueError as exc:
            result['errors'].append('分享链接解析失败：%s' % str(exc)[:80])
        if share_links:
            transfer_cfg = store.get('transfer_config', {}) or {}
            try:
                ids = await app.state.organizer.transfer.enqueue(
                    '\n'.join(share_links), transfer_cfg.get('target_cid', ''),
                    bool(transfer_cfg.get('auto_organize', False)), 'web')
                result['share'] = ids
            except Exception as exc:
                result['errors'].append('分享转存：%s' % str(exc)[:80])

        groups = {'magnet': [], 'ed2k': [], 'direct': []}
        for url in links_in(text):
            low = url.lower()
            if low.startswith('magnet:'):
                groups['magnet'].append(url)
            elif low.startswith('ed2k://'):
                groups['ed2k'].append(url)
            elif low.startswith(('http://', 'https://', 'ftp://')) and '115.com/s/' not in low:
                groups['direct'].append(url)
        cfg = config_value()
        post = app.state.organizer.offline
        for kind, urls in groups.items():
            if not urls:
                continue
            if kind == 'magnet':
                target, label, routes = cfg.get('magnet_cid'), cfg.get('magnet_label'), cfg.get('magnet_routes')
            elif kind == 'ed2k':
                target, label, routes = cfg.get('ed2k_cid'), cfg.get('ed2k_label'), cfg.get('ed2k_routes')
            else:
                target, label, routes = cfg.get('target_cid'), cfg.get('target_label'), []
            target, label = route_target(routes, text, target or '', label or '')
            if not target:
                result['errors'].append('%s 未配置目录，已跳过 %d 个链接' % (kind, len(urls)))
                continue
            try:
                sid = await post.submit(urls, target, label,
                                        organize=organize_flag_for(cfg, kind, 'web'))
                result['offline'].append(sid)
            except CloudError as exc:
                result['errors'].append('%s 提交失败：%s' % (kind, str(exc)[:80]))
        return result

    @app.get('/api/v1/offline/config', dependencies=[Depends(admin)])
    async def config_get():
        return {'config': config_value()}

    @app.put('/api/v1/offline/config', dependencies=[Depends(admin)])
    async def config_put(body: OfflineConfig):
        old = config_value()
        for kind in ('magnet', 'ed2k'):
            for suffix in ('cid', 'label'):
                field = 'bot_' + kind + '_' + suffix
                if getattr(body, field) is None:
                    setattr(body, field, old.get(field) or '')
        for field in ('target_cid', 'magnet_cid', 'ed2k_cid', 'bot_magnet_cid', 'bot_ed2k_cid'):
            cid = getattr(body, field)
            if cid:
                if not cid.isdigit() or int(cid) <= 0:
                    raise HTTPException(422, '请选择一个非根目录作为离线下载目录。')
                await app.state.cloud.directory(cid)
        value = body.model_dump()
        value['directories_v2'] = True
        store.set_many({'offline_config': value})
        return await config_get()

    @app.post('/api/v1/offline/jobs', dependencies=[Depends(admin)])
    async def submit(body: OfflineInput):
        urls = _split(body.text)
        if not urls:
            raise HTTPException(422, '请填写至少一个链接。')
        ensure_link_count(urls)
        for url in urls:
            if not url.lower().startswith(ALLOWED_PREFIXES):
                raise HTTPException(422, '只支持磁力、电驴、HTTP/HTTPS/FTP 链接：' + url[:60])
        seen = links_in(body.text)
        cfg = config_value()
        magnets = [u for u in seen if u.lower().startswith('magnet:')]
        ed2ks = [u for u in seen if u.lower().startswith('ed2k://')]
        others = [u for u in seen if not u.lower().startswith(('magnet:', 'ed2k://'))]
        kinds = sum(1 for group in (magnets, ed2ks, others) if group)
        if kinds > 1:
            raise HTTPException(422, '磁力、电驴与直链的保存目录不同，请分开提交，便于按批次整理。')
        if magnets:
            target, label = cfg.get('magnet_cid'), cfg.get('magnet_label')
            routes = cfg.get('magnet_routes')
        elif ed2ks:
            target, label = cfg.get('ed2k_cid'), cfg.get('ed2k_label')
            routes = cfg.get('ed2k_routes')
        else:
            target, label, routes = cfg.get('target_cid'), cfg.get('target_label'), []
        target, label = route_target(routes, body.text, target or '', label or '')
        post = app.state.organizer.offline
        kind = 'magnet' if magnets else ('ed2k' if ed2ks else 'direct')
        try:
            sid = await post.submit(seen, target or '', label or '',
                                    organize=organize_flag_for(cfg, kind, 'web'))
        except CloudError as exc:
            raise HTTPException(502, str(exc))
        auto = '，完成后自动定位本批产物并整理。' if cfg.get('auto_organize', True) else '。'
        return {'count': len(seen), 'submission_id': sid, 'message': '已提交 %d 个离线任务到%s' % (len(seen), label or target) + auto}

    @app.get('/api/v1/offline/submissions', dependencies=[Depends(admin)])
    async def submissions(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        total = store.db.execute('SELECT count(*) FROM offline_submissions').fetchone()[0]
        rows = store.db.execute(
            'SELECT * FROM offline_submissions ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return {'items': [dict(r) for r in rows], 'total': total,
                'has_more': offset + len(rows) < total, 'offset': offset}

    @app.get('/api/v1/offline/tasks', dependencies=[Depends(admin)])
    async def tasks():
        rows, running = await app.state.cloud.offline_tasks()
        items, total = _tasks(rows)
        return {'items': items, 'total': total, 'running': running}
