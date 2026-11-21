"""Durable station notices; transient Telegram failures are retried with backoff."""
import asyncio
import random
import json
import os
import time
import re
from functools import lru_cache
from pathlib import Path

import httpx
from .cards import media_card
from .message_templates import compact, transfer_completed, sync_task_context


CATEGORY_DEFAULTS={'media':True,'metadata':True,'transfer':True,'strm_summary':True,'strm_files':True,'errors':True,'emby':False,'task':False,'general':True,'cloud':True,'silent':False}
# 0.8.61：只下线 `task`（普通整理任务完成回执）。它默认就关着、从来没人开过，页面也不再提供开关；
# 这里强制关闭，避免旧配置里残留的 true 把它重新打开。
# 海报卡片（media）必须保留：卡片自带入库状态，drain_media 会把「⏳ 待核实」原位改成「✅ 已入库」，
# 所以不需要再单独开「Emby 入库检查结果」那类通知。
# `metadata` 也必须保留：它控制 STRM 汇总里「🧩 文件：…」那行统计（见 file_line）。
RETIRED_CATEGORIES=('task',)

def category_for(key):
    if key.startswith('media:'):return 'media'
    if key.startswith('strm:'):return 'strm_files' if ':files:' in key else 'strm_summary'
    if key.startswith('emby'):return 'emby'
    return 'general'

def clipped(text,limit):
    return text.encode('utf-16-le')[:limit*2].decode('utf-16-le',errors='ignore')


def strm_root():
    """STRM 输出根目录：与 main.create_app 使用同一个环境变量契约。"""
    return os.environ.get('STRIMKEEP_STRM_DIR', '/strm')


# 明细里跟着 STRM 一起列出的附属文件：只跟 nfo。
# 媒体信息 json 目前还补不齐，先不进明细，避免出现「列了却没有」的错觉；
# 海报（jpg/png）按目录只出一张，跟条目对不上，也不进明细。
SIDECAR_SUFFIXES = ('.nfo',)


def sidecar_paths(paths, root):
    """把 .strm 展开成「本体 + 同目录附属」，用于通知明细。"""
    result, seen = [], set()
    for rel in paths:
        rel = str(rel)
        if rel in seen:
            continue
        seen.add(rel)
        result.append(rel)
        if not rel.lower().endswith('.strm'):
            continue
        stem = rel[:-5]
        for suffix in SIDECAR_SUFFIXES:
            candidate = stem + suffix
            if candidate in seen:
                continue
            try:
                if (Path(root) / candidate).is_file():
                    seen.add(candidate)
                    result.append(candidate)
            except OSError:
                continue
    return result


def _units(text):
    """Telegram 按 UTF-16 码元计长度，不是字符数。"""
    return len(text.encode('utf-16-le')) // 2

@lru_cache(maxsize=8192)
def natural_path_key(path):
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r'(\d+)', path))


def _split_units(text, limit):
    parts, part, used = [], '', 0
    for char in text:
        width = _units(char)
        if used + width > limit:
            parts.append(part); part, used = '', 0
        part += char; used += width
    if part: parts.append(part)
    return parts


def strm_tree(paths, max_lines=20, max_units=3400):
    """把 STRM 相对路径渲染成「目录行 + 树状文件」的文本分片。

    同一目录只出现一次表头，文件用 ├── / └── 缩进；一个目录跨片时重复表头，
    让每条消息都能独立看懂自己在哪个目录下。首段是库根（影视媒体库/分享影视库），
    显示时去掉，只留「剧集 / 欧美剧集 / 片名 / Season 1」。
    """
    groups, order = {}, []
    for path in paths:
        head, _, name = path.rpartition('/')
        if head not in groups:
            groups[head] = []
            order.append(head)
        groups[head].append(name)

    chunks, current = [], []

    def flush():
        if current:
            text = '\n'.join(current).strip('\n')
            if text: chunks.append(text)
            current.clear()

    for position, head in enumerate(order):
        chain = [part for part in head.split('/') if part]
        label = ' / '.join(chain[1:]) if len(chain) > 1 else (chain[0] if chain else '')
        names = groups[head]
        lines = [('└── ' if i == len(names) - 1 else '├── ') + name for i, name in enumerate(names)]
        index = 0
        while index < len(names):
            block = [lines[index]]
            name = names[index]
            if (name.lower().endswith('.strm') and index + 1 < len(names)
                    and names[index + 1] == name[:-5] + '.nfo'):
                block.append(lines[index + 1])
            prefix = ([''] if current and position and index == 0 else [])
            if label and (index == 0 or not current):
                prefix.append(label)
            page = [*current, *prefix, *block]
            if len(page) > max_lines or _units('\n'.join(page)) > max_units:
                flush()
                page = ([label] if label else []) + block
            if _units('\n'.join(page)) > max_units:
                # Extremely long legacy paths can exceed an entire Telegram page.
                chunks.extend(_split_units('\n'.join(page), max_units))
            else:
                current[:] = page
            index += len(block)
    flush()
    return chunks


def sync_scope_lines(config):
    """只有真的按规则挡掉了视频才提示。

    附属文件/非视频本来就不生成 STRM，每天报「146 个」只是噪音。
    """
    skipped_videos = int(config.get('sync_filter_count') or 0)
    return f'按生成规则跳过 {skipped_videos} 个视频（不生成 STRM）' if skipped_videos else ''


def strm_detail_paths(items):
    """明细只列「新增」和「洗版替换」两类，没变化的完全不列。

    洗版批次里其余几十集多是重写或重新发布（地址换了但集没变），逐条列出会把明细
    刷满，海报和明细也对不上号。用 strm_created / share_wash_replaced 判定：两个
    标志都不成立的条目直接跳过，只在汇总里报数量。
    返回 (新增列表, 洗版替换列表)。
    """
    created, replaced = [], []
    for _, plan in items:
        path = plan.get('relative_path') or ''
        if not path.endswith('.strm') or plan.get('strm_filtered'):
            continue
        if plan.get('share_wash_replaced') or (plan.get('wash') or {}).get('decision') == 'replace':
            replaced.append(path)
        elif plan.get('strm_created'):
            created.append(path)
    return (sorted(set(created), key=natural_path_key),
            sorted(set(replaced), key=natural_path_key))


def strm_summary_lines(items, removed=(), replaced=(), pending=()):
    """批次汇总口径：写入 = 新增 + 更新 + 洗版替换，数字必须对得上。

    现场：一次分享整理写入 30 个 STRM，其中 25 个是同路径更新（播放地址/NFO
    变了），只有 5 个是新路径，汇总却只写「新增 5 个 / 清理 0」——看起来像丢了
    25 个。更新项按设计不逐条列明细，但数量必须出现在汇总里。
    """
    created, replaced_paths = strm_detail_paths(items)
    created_set, replaced_set = set(created), set(replaced_paths)
    written = updated = rewritten = 0
    for _, plan in items:
        path = str(plan.get('relative_path') or '')
        if not path.lower().endswith('.strm') or plan.get('strm_filtered'):
            continue
        written += 1
        if path in created_set or path in replaced_set:
            continue
        if plan.get('strm_rewritten'):
            rewritten += 1
        else:
            updated += 1
    lines = []
    if written:
        lines.append(f'写入 STRM：{written} 个')
    if created:
        lines.append(f'新增 STRM：{len(created)} 个')
    if rewritten:
        lines.append(f'重写 STRM：{rewritten} 个（同路径覆盖，明细略）')
    if updated:
        lines.append(f'更新 STRM：{updated} 个（地址/元数据变化，明细略）')
    if replaced_paths:
        lines.append(f'洗版替换：{len(replaced_paths)} 个')
    if removed:
        lines.append(f'清理 STRM：{len(removed)} 个文件')
    if replaced:
        lines.append(f'同路径覆盖：{len(replaced)} 个 STRM')
    if pending:
        lines.append(f'清理结果待核实：{len(pending)} 个 STRM')
    return lines, created, replaced_paths, written


def strm_detail_titles(chunks, total, label='STRM 文件明细', source=''):
    """每条明细的标题：序号 + 本片覆盖的条目区间。

    `total` 是**条目数**（.strm 的个数），不是正文行数——明细里每个条目后面
    还会跟同目录的 .nfo，按行数算会出现「1-28/28」却只有 14 集对不上。
    """
    titles, first = [], 1
    for index, chunk in enumerate(chunks, 1):
        filed = sum(1 for line in chunk.split('\n')
                    if line.startswith(('├── ', '└── ')) and line.rstrip().endswith('.strm'))
        span = f' ({first}-{first + filed - 1}/{total})' if filed else ''
        titles.append(f'📄 {source + " · " if source else ""}{label} {index}/{len(chunks)}{span}')
        first += filed
    return titles


def strm_entry_count(paths):
    """条目数 = .strm 个数；附属文件（nfo）不单独计数。"""
    return sum(1 for path in paths if str(path).lower().endswith('.strm'))


def strm_source(config):
    kind = config.get('job_kind', 'organize')
    if kind == 'share_organize': return '分享 STRM'
    if kind == 'strm_sync': return '网盘同步'
    if config.get('transfer_parent'): return '网盘转存整理'
    if str(config.get('origin') or '').startswith('offline:'): return '离线下载整理'
    return '网盘整理'


RETRY_LIMIT = 30
RETRY_BASE_SECONDS = 30
RETRY_CAP_SECONDS = 900


class Notifications:
    def __init__(self, store, http):
        self.store, self.http = store, http
        self.report_gate = asyncio.Lock()
        self.delivery_gate = asyncio.Lock()
        self.send_gate = asyncio.Lock()
        self.next_send = 0.0
        self.media_gate = asyncio.Lock()
        self.channel_photo = None
        self._verified_cards_requeued = False
        with self.store.db:
            self.store.db.execute('CREATE TABLE IF NOT EXISTS notification_dependencies(event_key TEXT PRIMARY KEY, depends_on TEXT NOT NULL)')
            self.store.db.execute('CREATE TABLE IF NOT EXISTS media_notice_outbox(job_id INTEGER PRIMARY KEY)')
            self.store.db.execute("CREATE TABLE IF NOT EXISTS media_notice_groups(event_key TEXT PRIMARY KEY,job_id INTEGER,items TEXT NOT NULL)")
            self.store.db.execute("CREATE TABLE IF NOT EXISTS telegram_message_refs(event_key TEXT PRIMARY KEY,chat_id TEXT NOT NULL,message_id INTEGER NOT NULL,photo INTEGER NOT NULL,edited TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0)")

    async def _deliver_webhook(self, title, message, category):
        """通用 Webhook：不参与 Telegram 状态机，也不新增台账表。"""
        config = self.store.get('notification_config', {})
        if not config.get('webhook_enabled') or not config.get('webhook_url'):
            return
        categories = {**CATEGORY_DEFAULTS, **config.get('categories', {})}
        if not categories.get(category, True):
            return
        from .webhook_notifier import WebhookNotifier
        notifier = WebhookNotifier(self.http, config['webhook_url'], config.get('webhook_secret', ''))
        try:
            await notifier.send(title, message)
        except Exception:
            # 通知旁路失败不能影响入库/整理主流程；Telegram 队列仍保留正式回执。
            pass

    async def publish(self, *args, **kwargs):
        async with self.delivery_gate:
            return await self._publish(*args, **kwargs)

    async def _publish(self, key, title, message, level='info', job_id=None, test=False, photo=None, chat_override=None, force=False, category=None, depends_on=None, telegram_options=None):
        config = self.store.get('notification_config', {})
        category=category or category_for(key)
        categories={**CATEGORY_DEFAULTS,**config.get('categories',{})}
        for retired in RETIRED_CATEGORIES:
            categories[retired]=False
        enabled = (config.get('telegram_enabled', False) and categories.get(category,True)) or test or force
        previous = self.store.db.execute('SELECT telegram_state,attempts FROM notifications WHERE event_key=?', (depends_on,)).fetchone() if depends_on else None
        waiting = bool(enabled and previous and (previous['telegram_state'] in ('inflight', 'waiting') or
                       (previous['telegram_state'] == 'unknown' and previous['attempts'] < RETRY_LIMIT)))
        initial = 'waiting' if waiting else ('inflight' if enabled else 'disabled')
        with self.store.db:
            cursor = self.store.db.execute('INSERT OR IGNORE INTO notifications(at,level,job_id,event_key,title,message,telegram_state,photo,telegram_options) VALUES(?,?,?,?,?,?,?,?,?)',
                (time.time(), level, job_id, key, title, message, initial, photo, json.dumps(telegram_options or {})))
            if cursor.rowcount and depends_on:
                self.store.db.execute('INSERT OR REPLACE INTO notification_dependencies VALUES(?,?)', (key, depends_on))
        if not cursor.rowcount: return 'duplicate'
        if not test:
            await self._deliver_webhook(title, message, category)
        if waiting: return 'waiting'
        state = 'disabled'
        if enabled:
            token, chat = config.get('bot_token'), chat_override or config.get('chat_id')
            state, detail = await self._deliver_notice(key, title, message, photo, chat, token, telegram_options)
            with self.store.db:
                self.store.db.execute(
                    'UPDATE notifications SET telegram_state=?,attempts=attempts+1,next_try=?,photo=? WHERE id=?',
                    (state, time.time() + self.backoff(1) if state == 'unknown' else 0, photo, cursor.lastrowid))
                if state=='sent' and isinstance(detail,int) and category=='media':
                    self.store.db.execute('INSERT OR REPLACE INTO telegram_message_refs(event_key,chat_id,message_id,photo) VALUES(?,?,?,?)',(key,str(chat),detail,int(bool(photo))))
        return state

    @staticmethod
    def backoff(attempts):
        """重试间隔：30 秒起，翻倍，封顶 15 分钟，再乘 ±15% 抖动。

        0.8.22：加抖动是为了打散重试节奏——多条通知同时失败时，固定间隔会让它们在
        同一秒一起重发，Telegram 侧限流时尤其明显（AWS《Exponential Backoff And Jitter》）。
        """
        base = min(RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1), RETRY_CAP_SECONDS)
        return min(RETRY_CAP_SECONDS, base * random.uniform(0.85, 1.15))

    async def _deliver_notice(self, key, title, message, photo, chat, token, telegram_options=None):
        values = {'telegram_options': telegram_options} if telegram_options else {}
        if key.startswith('strm:') and ':group:' in key:
            prefix, _, group = key.partition(':group:')
            group = group.removesuffix(':summary') if group.endswith(':summary') else group.rsplit(':files:', 1)[0]
            ref = self.store.db.execute(
                'SELECT message_id FROM telegram_message_refs WHERE event_key=? AND chat_id=?',
                (f'show_done:{prefix[5:]}:{group}', str(chat))).fetchone()
            if ref:
                values['reply_to'] = ref['message_id']
        return await self._deliver(title, message, photo, chat, token, **values)

    async def _deliver(self, title, message, photo, chat, token, *, reply_to=None, telegram_options=None):
        """一次发送。unknown 表示可重试（网络/5xx/429），failed 表示对方明确拒收（4xx）。"""
        if not token or not chat:
            return 'failed', 'missing token or chat'
        try:
            options = telegram_options or {}
            payload = {'chat_id': chat}
            if reply_to is not None:
                payload['reply_parameters'] = {'message_id': reply_to, 'allow_sending_without_reply': True}
            if options.get('post_url'):
                payload['reply_markup'] = {'inline_keyboard': [[{'text': '查看原帖', 'url': options['post_url']}]]}
            text = title + '\n' + message
            file = None
            source = options.get('photo_source')
            response = None
            copy_source = options.get('copy_source')
            if copy_source and not options.get('spoiler'):
                response = await self.send(token, 'copyMessage', {**payload, **copy_source, 'caption': clipped(text, 1000)})
                if response.status_code in (400, 403):
                    if any(mark in response.text.lower() for mark in ('protected content', 'forwards_restricted', "can't be forwarded")):
                        source = photo = None
                    response = None
            if response is None and source and self.channel_photo:
                try:
                    file = await asyncio.wait_for(self.channel_photo(source['channel'], source['id']), timeout=9)
                except Exception:
                    pass
            if response is None and (file or photo):
                photo_payload = {**payload, 'caption': clipped(text, 1000)}
                if options.get('spoiler'):
                    photo_payload['has_spoiler'] = True
                if file:
                    response = await self.send(token, 'sendPhoto', photo_payload, file=file)
                else:
                    response = await self.send(token, 'sendPhoto', {**photo_payload, 'photo': photo})
            # If the source photo has expired, still deliver the caption and post link.
            if response is None or (response.status_code == 400 and options.get('post_url')):
                payload.update(text=clipped(text, 4000), link_preview_options={'is_disabled': True})
                response = await self.send(token, 'sendMessage', payload)
            response.raise_for_status()
            data = response.json()
            if isinstance(data,dict) and data.get('ok') is True:
                result=data.get('result')
                return 'sent', result.get('message_id') if isinstance(result,dict) else None
            return 'unknown', ''
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return ('unknown' if code >= 500 or code == 429 else 'failed'), f'HTTP {code}'
        except (httpx.HTTPError, ValueError) as exc:
            from .telegram_security import safe_telegram_error
            return 'unknown', safe_telegram_error(exc, token)[:80]

    async def retry(self, now=None, limit=20):
        async with self.delivery_gate:
            return await self._retry(now, limit)

    async def _retry(self, now=None, limit=20):
        """重发临时失败的通知；对方明确拒收的不重发，超过上限的不再重发。"""
        now = time.time() if now is None else now
        config = self.store.get('notification_config', {})
        token, chat = config.get('bot_token'), config.get('chat_id')
        if not config.get('telegram_enabled') or not token or not chat:
            return 0
        sent = 0
        for _ in range(limit):
            # 每次选择已到时且前一条已结束的最早消息；成功后同一轮继续放行后续分段。
            row = self.store.db.execute("""SELECT n.* FROM notifications n
                LEFT JOIN notification_dependencies d ON d.event_key=n.event_key
                LEFT JOIN notifications p ON p.event_key=d.depends_on
                WHERE ((n.telegram_state='unknown' AND n.attempts<? AND n.next_try<=?)
                    OR n.telegram_state='waiting')
                  AND (p.event_key IS NULL OR p.telegram_state IN ('sent','disabled','failed')
                    OR (p.telegram_state='unknown' AND p.attempts>=?))
                ORDER BY n.id LIMIT 1""", (RETRY_LIMIT, now, RETRY_LIMIT)).fetchone()
            if not row:
                break
            with self.store.db:
                self.store.db.execute("UPDATE notifications SET telegram_state='inflight' WHERE id=?", (row['id'],))
            state, detail = await self._deliver_notice(
                row['event_key'], row['title'], row['message'], row['photo'], chat, token,
                json.loads(row['telegram_options']))
            attempts = row['attempts'] + 1
            with self.store.db:
                self.store.db.execute(
                    'UPDATE notifications SET telegram_state=?,attempts=?,next_try=? WHERE id=?',
                    (state, attempts, now + self.backoff(attempts) if state == 'unknown' else 0, row['id']))
                if state=='sent' and isinstance(detail,int) and (row['event_key'].startswith('show_done:') or row['event_key'].startswith('media:')):
                    self.store.db.execute('INSERT OR REPLACE INTO telegram_message_refs(event_key,chat_id,message_id,photo) VALUES(?,?,?,?)',(row['event_key'],str(chat),detail,int(bool(row['photo']))))
            sent += 1 if state == 'sent' else 0
        return sent

    async def send(self,token,method,payload,*,file=None):
        async with self.send_gate:
            await asyncio.sleep(max(0,self.next_send-time.monotonic()))
            try:
                url = 'https://api.telegram.org/bot'+token+'/'+method
                if file:
                    form = {key: value if isinstance(value, str) else json.dumps(value)
                            for key, value in payload.items()}
                    return await self.http.post(url, data=form, files={'photo': ('poster.jpg', file, 'image/jpeg')}, timeout=15)
                return await self.http.post(url, json=payload, timeout=15)
            finally:self.next_send=time.monotonic()+1.1

    async def job(self, row):
        async with self.report_gate:
            await self._job(row)

    async def group_strm(self, job_id, group_key, items, *, card=None):
        async with self.delivery_gate:
            if card:
                title, message, photo = card
                await self._publish(f'show_done:{job_id}:{group_key}', title, message, job_id=job_id,
                                    photo=photo, category='media')
            await self._group_strm(job_id, group_key, items)

    def metadata_counts(self, job_id, ids):
        """本批真正写出的 .strm / .nfo / .json 数量。"""
        if not ids:
            return {}
        marks=','.join('?' for _ in ids)
        # 只报 .strm / .nfo / .json 三样：剧集海报是一集一张、数量大且没信息量，
        # 放进来只会把明细刷乱，所以不统计也不展示。
        counts={'strm':0,'nfo':0,'nfo_show':0,'json':0}
        relatives=set()
        for (plan,) in self.store.db.execute(
                f'SELECT plan FROM organize_items WHERE job_id=? AND id IN ({marks})',(job_id,*ids)):
            parsed=json.loads(plan)
            if parsed.get('relative_path'):relatives.add(parsed['relative_path'])
            if (str(parsed.get('relative_path','')).endswith('.strm')
                    and not parsed.get('strm_filtered')):
                counts['strm']+=1
            for name in (parsed.get('metadata_files') or []):
                suffix=str(name).rsplit('.',1)[-1].casefold()
                if suffix=='nfo':
                    # 剧集级 tvshow.nfo 单独分出来：一部新剧第一集会同时写
                    # 「每集 nfo + tvshow.nfo」，不拆开就会被看成数量对不上。
                    if str(name).endswith('tvshow.nfo'):counts['nfo_show']+=1
                    else:counts['nfo']+=1
                elif suffix=='json':counts['json']+=1
        # 媒体信息落盘就是 STRM 旁边的 .json，算进同一项。
        media_paths=set()
        for (target,) in self.store.db.execute(
                "SELECT target FROM native_media_info_queue WHERE job_id=? AND state='done'", (job_id,)):
            try:relative=json.loads(target).get('relative')
            except (ValueError,TypeError,AttributeError):continue
            if relative in relatives:media_paths.add(relative)
        counts['json']+=len(media_paths)
        return counts

    def metadata_line(self, job_id, ids):
        """NFO / 海报 / 媒体信息 的统计行；受通知分类 metadata 开关控制。"""
        counts=self.metadata_counts(job_id,ids)
        produced=[]
        nfo_total=counts.get('nfo',0)+counts.get('nfo_show',0)
        part=f'NFO 写入 {nfo_total}' if nfo_total else 'NFO 本批未写入'
        if counts.get('nfo_show'):part+=f'（剧集级 {counts["nfo_show"]}）'
        produced.append(part)
        if counts.get('json'):
            produced.append(f'媒体信息 {counts["json"]}')
        if not produced:
            return ''
        settings=self.store.get('notification_config',{})
        categories={**CATEGORY_DEFAULTS,**settings.get('categories',{})}
        return '🧩 文件：'+' · '.join(produced) if categories.get('metadata',True) else ''

    async def _group_strm(self, job_id, group_key, items):
        """Report each completed movie/season once, without waiting for the batch."""
        ids = [item_id for item_id, plan in items]
        if not ids:
            return
        marks = ','.join('?' for _ in ids)
        changes = self.store.db.execute(f'SELECT action,path FROM strm_change_items WHERE job_id=? AND item_id IN ({marks})', (job_id,*ids)).fetchall()
        removed = sorted({r['path'] for r in changes if r['action']=='removed'},key=natural_path_key)
        replaced = {r['path'] for r in changes if r['action']=='replaced'}
        pending = sorted({r['path'] for r in changes if r['action']=='remove_pending'} - set(removed),key=natural_path_key)
        lines, created, replaced_paths, written = strm_summary_lines(items, removed, replaced, pending)
        if written or removed or pending:
            prefix=f'strm:{job_id}:group:{group_key}'
            source=strm_source(json.loads(self.store.db.execute('SELECT config FROM organize_jobs WHERE id=?',(job_id,)).fetchone()[0]))
            plan = items[0][1]
            details = plan.get('details') or {}
            subject = compact(details.get('name') or details.get('title'), 85)
            season = (plan.get('hint') or {}).get('season')
            if subject and isinstance(season, int):
                subject += f' · S{season:02d}'
            scope = f'{subject} · 任务 #{job_id}' if subject else f'任务 #{job_id}'
            summary = '\n'.join([scope, *lines])
            meta_line=self.metadata_line(job_id,ids)
            if meta_line:summary+='\n'+meta_line
            previous_key = f'show_done:{job_id}:{group_key}'
            summary_key = prefix+':summary'
            await self._publish(summary_key,f'📦 {source} · STRM 批量结果',summary,
                                job_id=job_id,category='strm_summary',depends_on=previous_key)
            previous_key = summary_key
            for kind,heading,values in [('created','🆕 新增 STRM',created),('replaced','🔄 洗版替换',replaced_paths),
                                        ('removed','🧹 已清理旧 STRM',removed),('pending','⚠️ 清理结果待核实',pending)]:
                if not values: continue
                # 新增/替换项把同目录的 nfo 一起列出来；清理项只记 STRM 本身。
                listed = sidecar_paths(values, strm_root()) if kind in ('created','replaced') else values
                count = strm_entry_count(listed)
                title_label = 'STRM 与 NFO 文件明细' if count != len(listed) else 'STRM 文件明细'
                chunks=strm_tree(listed)
                for i,(chunk,title) in enumerate(zip(chunks,strm_detail_titles(chunks,count,title_label,source)),1):
                    file_key = prefix+':files:'+kind+':'+str(i)
                    await self._publish(file_key,title,scope+'\n'+heading+'\n'+chunk,
                                        job_id=job_id,category='strm_files',depends_on=previous_key)
                    previous_key = file_key
        with self.store.db:
            self.store.db.executemany('INSERT OR IGNORE INTO strm_reported_items VALUES(?,?)', [(job_id,i) for i in ids])
            self.store.db.execute('DELETE FROM strm_group_outbox WHERE job_id=? AND group_key=?',(job_id,group_key))

    async def _job(self, row):
        state = row['state']
        if state not in ('completed','partial','needs_review','waiting_login','cancelled'): return
        if (row['stage'] or '') in ('已人工结项', '已自动结项', '账本已闭环'):
            # 结项只是把账本收口，不是「这批刚入库」。现场 368 条历史入库通知
            # 就是这么补发出去的（每条间隔 1.1 秒，刷了六分钟）。
            self.store.jobs.set_notify_state(row['id'], 'not_needed')
            self.store.event('organize', f'任务 {row["id"]} 已结项，不再补发历史入库通知。')
            return
        if state == 'cancelled':
            # 失败/中断的任务再补一条「已取消」只是把同一件事说两遍，只留操作记录。
            self.store.event('organize', f'任务 {row["id"]} 已取消。')
            self.store.jobs.set_notify_state(row['id'], 'not_needed')
            return
        if state in ('completed','partial','needs_review','waiting_login'):
            for entry in self.store.db.execute('SELECT group_key,items FROM strm_group_outbox WHERE job_id=?',(row['id'],)).fetchall():
                ids=json.loads(entry['items'])
                if not ids:continue
                marks=','.join('?' for _ in ids)
                items=self.store.db.execute(f'SELECT id,original,plan FROM organize_items WHERE job_id=? AND id IN ({marks}) AND state=? ORDER BY id', (row['id'],*ids,'done')).fetchall()
                if len(items)==len(ids):
                    plans = [(item['id'], json.loads(item['plan'])) for item in items]
                    card_key = f'show_done:{row["id"]}:{entry["group_key"]}'
                    card = None
                    if not self.store.db.execute('SELECT 1 FROM notifications WHERE event_key=?', (card_key,)).fetchone():
                        config = json.loads(row['config'])
                        card = media_card(
                            [(json.loads(item['original']), plan) for item, (_, plan) in zip(items, plans)],
                            shared=config.get('job_kind') == 'share_organize',
                            transferred=bool(config.get('transfer_parent')),
                            emby_status='pending' if config.get('notify_emby') else '')
                    await self.group_strm(row['id'], entry['group_key'], plans, card=card)
            await self.media_report(row)
        kind=json.loads(row['config']).get('job_kind','organize')
        empty=kind=='organize' and state=='completed' and not self.store.db.execute('SELECT 1 FROM organize_items WHERE job_id=?',(row['id'],)).fetchone()
        filtered = json.loads(row['config']).get('scan_filter', {})
        if empty and (not filtered.get('scanned') or json.loads(row['config']).get('scheduled')):
            # 空扫描只写实时日志，不进通知中心、也不发机器人。
            # 必须把 notify_state 置为终态：调度器是按“通知表里没有该任务记录”
            # 来重试的，不标记就会被每一轮重复通知，把事件流刷满。
            self.store.event('organize',f'任务 {row["id"]}：' + (row['message'] if filtered.get('scanned') else '未发现待整理视频。'))
            self.store.jobs.set_notify_state(row['id'], 'not_needed')
            return
        title = {'completed':'✅ 处理完成','partial':'⚠️ 部分文件未完成','needs_review':'⚠️ 任务暂未完成','waiting_login':'🔑 115 需要重新登录','cancelled':'任务已取消'}[state]
        if kind=='organize' and state=='completed' and empty and filtered.get('scanned'):
            title='⏭ 已按格式规则跳过'
        if kind=='transfer' and state=='completed':title='📥 转存完成'
        elif kind=='transfer':title={'partial':'⚠️ 网盘转存待核查','needs_review':'⚠️ 网盘转存待核查','waiting_login':'🔑 网盘转存等待登录'}.get(state,title)
        elif kind=='share_organize':
            title={'completed':'✅ 分享 STRM 整理完成','partial':'⚠️ 分享 STRM 部分待处理','needs_review':'⚠️ 分享 STRM 待核查','waiting_login':'🔑 分享 STRM 等待登录'}.get(state,title)
            if state=='completed' and row['stage']=='分享 STRM 已生成':title='✅ 分享 STRM 已生成，等待整理'
        if kind=='strm_sync' and json.loads(row['config']).get('sync_mode')=='sweep':
            await self.publish(f'strm:{row["id"]}:summary','🧹 STRM 本地巡检',row['message'] or '',
                               'success' if state=='completed' else 'warning',row['id'],category='strm_summary')
            return
        if kind=='strm_sync' and state in ('completed','partial'):
            entries=self.store.db.execute('SELECT original,plan,state FROM organize_items WHERE job_id=?',(row['id'],)).fetchall()
            c=json.loads(row['config']); labels=c.get('directory_labels') or {}
            stats=c.get('sync_directory_stats') or {}
            groups={}
            for entry in entries:
                root=(json.loads(entry['original']) or {}).get('_root') or '未标注来源'
                groups.setdefault(root,[]).append((json.loads(entry['plan']),entry['state']))
            roots=c.get('sync_effective_roots') or list(groups)
            changed=sum(bool(plan.get('output_changed')) for items in groups.values() for plan,_ in items)
            unfinished=sum(item_state not in ('done','skipped') for items in groups.values() for _,item_state in items)
            duration=max(0,float(row['updated'])-float(row['created']))
            reused=sum(bool(plan.get('sync_unchanged')) for items in groups.values() for plan,_ in items)
            lines=[sync_task_context(c).rstrip(), '结果：完成' if state=='completed' else '结果：部分未完成', '']
            if len(roots)==1:
                cid=roots[0]; detail=stats.get(cid) or {}
                lines.append('☁️ 云盘目录：'+str(labels.get(cid) or detail.get('name') or cid))
                if detail.get('local_path'):lines.append('📁 本地目录：'+str(detail['local_path']))
                lines.append('')
            if c.get('sync_mode')=='cached':lines.append('扫描方式：复用近期清单')
            elif stats:
                dirs=sum(int(item.get('directories',0) or 0) for item in stats.values())
                files=sum(int(item.get('files',0) or 0) for item in stats.values())
                label='扫描目录' if all(item.get('directories_exact',True) for item in stats.values()) else '文件路径涉及目录'
                lines.append(f'📊 {label}：{dirs} 个 · 扫描条目：{files} 个')
                seconds=sum(float(item.get('scan_seconds',0) or 0) for item in stats.values())
                if seconds:lines.append(f'目录读取耗时：{seconds:.2f} 秒')
            created = sum(bool(plan.get('sync_created')) for items in groups.values() for plan, _ in items)
            updated = sum(bool(plan.get('sync_updated')) for items in groups.values() for plan, _ in items)
            att_created = sum(bool(plan.get('sync_attachment_created')) for items in groups.values() for plan, _ in items)
            att_updated = sum(bool(plan.get('sync_attachment_updated')) for items in groups.values() for plan, _ in items)
            lines.extend(['', f'🎬 STRM：新增 {created} 个 · 更新 {updated} 个 · 未变化 {reused} 个',
                          f'核对视频：{len(entries)} 个', f'清理本地：{c.get("sync_removed",0)} 个文件'])
            if att_created or att_updated:
                lines.append(f'附属文件：写入 {att_created + att_updated} 个（新增 {att_created}、更新 {att_updated}）')
            metadata=list((c.get('sync_metadata_stats') or {}).values())
            downloaded=sum(int(m.get('downloaded',0) or 0) for m in metadata)
            existing=sum(int(m.get('existing',0) or 0) for m in metadata)
            skipped=sum(int(m.get('skipped',0) or 0) for m in metadata)
            failed=sum(int(m.get('failed',0) or 0) for m in metadata)
            if downloaded or existing or skipped or failed:
                parts=[f'新增 {downloaded} 个']
                if existing:parts.append(f'已存在 {existing} 个')
                if skipped:parts.append(f'跳过 {skipped} 个')
                if failed:parts.append(f'失败 {failed} 个')
                lines.append('附属文件：'+' · '.join(parts))
                types={key:sum(int((m.get('types') or {}).get(key,0) or 0) for m in metadata)
                       for key in ('nfo','image','subtitle','json','other')}
                labels_by_type={'nfo':'NFO','image':'图片','subtitle':'字幕','json':'JSON','other':'其他'}
                breakdown=[f'{labels_by_type[key]} {count}' for key,count in types.items() if count]
                if breakdown:lines.append('新增类型：'+' · '.join(breakdown))
            # 云盘条目里除了视频还有字幕/NFO/图片，也会被格式规则跳过一部分；
            # 这行把差额说清楚，避免「扫描 960、核对 877」看起来像少扫了。
            total = sum(int(item.get('files', 0) or 0) for item in (stats or {}).values())
            if total and total > len(entries):
                filtered = len(c.get('sync_filtered_ids') or [])
                extra = f'，其中按规则跳过 {filtered} 个' if filtered else ''
                lines.append(f'扫描条目里含字幕/NFO/图片等非视频 {total - len(entries)} 个{extra}')
            if unfinished:lines.append(f'待处理：{unfinished} 个')
            if len(roots)>1:
                lines.append('目录：')
                for cid in roots[:5]:
                    name=str(labels.get(cid) or (stats.get(cid) or {}).get('name') or cid)[:80]
                    items=groups.get(cid,[])
                    count=sum(bool(plan.get('output_changed')) for plan,_ in items)
                    lines.append(f'· {name}：核对 {len(items)}，变化 {count}')
                if len(roots)>5:lines.append(f'其余 {len(roots)-5} 个目录请查看任务详情')
            started = c.get('sync_execution_started_at')
            if isinstance(started, (int, float)) and not isinstance(started, bool) and float(row['created']) <= started <= float(row['updated']):
                elapsed = float(row['updated']) - started
                lines.append(f'\n⏱ 执行耗时：{elapsed:.2f} 秒')
                if duration - elapsed >= 1:
                    lines.append(f'任务总耗时：{duration:.2f} 秒')
            else:
                lines.append(f'\n⏱ 任务总耗时：{duration:.2f} 秒')
            await self.publish(f'strm:{row["id"]}:sync-summary','✅ STRM 同步完成' if state=='completed' else '⚠️ STRM 同步未全部完成',
                '\n'.join(lines).strip(),
                'success' if state=='completed' else 'warning',row['id'],category='strm_summary',
                depends_on=f'strm:{row["id"]}:start')
            if state=='completed':
                return
        category='transfer' if kind=='transfer' or (kind=='organize' and json.loads(row['config']).get('transfer_parent') and empty and filtered.get('scanned')) else 'errors' if state in ('partial','needs_review','waiting_login') else 'task'
        message=row['message'] or row['stage']
        if kind=='transfer' and state=='completed':
            transfer_config=json.loads(row['config'])
            # 目录名按当前配置解析：用户换了目录或改了名，通知里不能还是旧名字。
            transfer_config['target_label']=self.store.directory_label(
                transfer_config.get('target_cid'), transfer_config.get('target_label',''))
            title,message=transfer_completed(row['id'],transfer_config,message)
        await self.publish(f'job:{row["id"]}:{state}',title,message,
                           'success' if state == 'completed' else 'warning',row['id'],category=category)
        if row['notify_state'] in ('failed','unknown'):
            await self.publish(f'emby:{row["id"]}', f'Emby 通知未确认 #{row["id"]}', '已保留本批输出，请检查 Emby 连接或手动扫描。', 'warning', row['id'])

    async def drain_media(self):
        """Edit the original Telegram card after Emby settles; never send a second card."""
        async with self.media_gate:
            if not self._verified_cards_requeued:
                # Recover cards left behind before verified results could requeue them.
                with self.store.db:
                    self.store.db.execute("""
                        INSERT OR IGNORE INTO media_notice_outbox(job_id)
                        SELECT g.job_id FROM media_notice_groups g
                        JOIN emby_deliveries d ON d.job_id=g.job_id
                        JOIN notifications n ON n.event_key=g.event_key
                        JOIN telegram_message_refs r ON r.event_key=g.event_key
                        WHERE d.state='verified' AND n.telegram_state='sent'
                          AND r.edited IN ('','unconfirmed')
                          AND (r.edited='unconfirmed' OR instr(n.message,'Emby 入库未确认')>0
                               OR instr(n.message,'Emby 部分入库')>0
                               OR instr(n.message,'入库待核实')>0)
                    """)
                self._verified_cards_requeued = True
            jobs=self.store.db.execute("SELECT j.* FROM media_notice_outbox o JOIN organize_jobs j ON j.id=o.job_id ORDER BY j.id LIMIT 20").fetchall()
            for job in jobs:
                config=json.loads(job['config'])
                if config.get('notify_emby') and self.store.db.execute('SELECT 1 FROM emby_outbox WHERE job_id=?',(job['id'],)).fetchone():continue
                delivery=self.store.db.execute('SELECT state,payload FROM emby_deliveries WHERE job_id=?',(job['id'],)).fetchone()
                targets=json.loads(delivery['payload']).get('targets',[]) if delivery else []
                confirmed={t.get('relative') for t in targets if t.get('confirmed')}
                final_job=job['state'] in ('completed','partial','needs_review','cancelled')
                settled=(job['notify_state'] not in ('pending','retrying','accepted','checking','inflight') and
                         (not delivery or delivery['state'] not in ('pending','retrying','accepted','checking','inflight')))
                waiting=not final_job or not settled
                groups=self.store.db.execute('SELECT * FROM media_notice_groups WHERE job_id=?',(job['id'],)).fetchall()
                for group in groups:
                    key=group['event_key']
                    notice=self.store.db.execute('SELECT * FROM notifications WHERE event_key=?',(key,)).fetchone()
                    if not notice: waiting=True;continue
                    if notice['telegram_state']=='unknown' and notice['attempts']<RETRY_LIMIT: waiting=True;continue
                    if notice['telegram_state']!='sent' or not config.get('notify_emby'):continue
                    ref=self.store.db.execute('SELECT * FROM telegram_message_refs WHERE event_key=?',(key,)).fetchone()
                    if not ref:
                        self.store.event('telegram',f"任务 {job['id']}：海报缺少 Telegram 消息编号，无法原位更新入库状态。")
                        continue
                    ids=json.loads(group['items'])
                    plans=[]
                    for item_id in ids:
                        item=self.store.db.execute('SELECT plan FROM organize_items WHERE job_id=? AND id=?',(job['id'],item_id)).fetchone()
                        if item:plans.append(json.loads(item['plan']).get('relative_path'))
                    is_confirmed=bool(plans and len(plans)==len(ids) and all(p and p in confirmed for p in plans))
                    if not is_confirmed and not (final_job and settled):
                        waiting=True;continue
                    status='verified' if is_confirmed else 'unconfirmed'
                    if ref['edited']==status:continue
                    if ref['next_try']>time.time():waiting=True;continue
                    pending_lines = ('⏳ 状态：Emby 入库待核实', '⏳ Emby：入库待核实')
                    warning_line = '⚠️ 状态：Emby 入库未确认，请检查媒体库'
                    partial_line = '⚠️ 状态：Emby 部分入库，未确认的条目请检查媒体库'
                    old_lines = (*pending_lines, warning_line, partial_line) if is_confirmed else pending_lines
                    new = '✅ 状态：Emby 已入库' if is_confirmed else warning_line
                    lines = notice['message'].split('\n')
                    updated = '\n'.join(new if line in old_lines else line for line in lines)
                    if updated == notice['message']:continue
                    cfg=self.store.get('notification_config',{})
                    token=cfg.get('bot_token')
                    if not token:
                        waiting=True;continue
                    updated_title=notice['title'] if status in ('verified','partial') else notice['title'].replace('🎉 新片入库：','🎬 整理完成：')
                    content=clipped(updated_title+'\n'+updated,1000 if ref['photo'] else 4000)
                    payload={'chat_id':ref['chat_id'],'message_id':ref['message_id']}
                    payload['caption' if ref['photo'] else 'text']=content
                    method='editMessageCaption' if ref['photo'] else 'editMessageText'
                    try:
                        response=await self.send(token,method,payload)
                        data=response.json()
                        unchanged=response.status_code==400 and isinstance(data,dict) and 'message is not modified' in str(data.get('description','')).lower()
                        if not unchanged:
                            response.raise_for_status()
                            if not isinstance(data,dict) or data.get('ok') is not True:raise ValueError('edit not confirmed')
                    except (httpx.HTTPError,ValueError) as exc:
                        attempts=ref['attempts']+1
                        permanent=isinstance(exc,httpx.HTTPStatusError) and 400<=exc.response.status_code<500 and exc.response.status_code!=429
                        if permanent or attempts>=RETRY_LIMIT:
                            self.store.event('telegram',f"任务 {job['id']}：海报状态更新失败，请在任务中心查看 Emby 结果。")
                            with self.store.db:self.store.db.execute('UPDATE telegram_message_refs SET edited=? WHERE event_key=?',('failed',key))
                        else:
                            waiting=True
                            with self.store.db:self.store.db.execute('UPDATE telegram_message_refs SET attempts=?,next_try=? WHERE event_key=?',(attempts,time.time()+self.backoff(attempts),key))
                        continue
                    with self.store.db:
                        self.store.db.execute('UPDATE notifications SET title=?,message=? WHERE event_key=?',(updated_title,updated,key))
                        self.store.db.execute('UPDATE telegram_message_refs SET edited=?,attempts=0,next_try=0 WHERE event_key=?',(status,key))
                if not waiting:
                    with self.store.db:self.store.db.execute('DELETE FROM media_notice_outbox WHERE job_id=?',(job['id'],))

    async def media_report(self, job):
        async with self.delivery_gate:
            await self._media_report(job)

    async def _media_report(self, job):
        groups={};paths=[]
        reported={r['item_id'] for r in self.store.db.execute('SELECT item_id FROM strm_reported_items WHERE job_id=?',(job['id'],))}
        grouped_ids = set()
        for event in self.store.db.execute("""SELECT g.items FROM media_notice_groups g
            JOIN notifications n ON n.event_key=g.event_key
            WHERE g.job_id=? AND g.event_key LIKE 'show_done:%' AND n.telegram_state!='failed'""", (job['id'],)):
            grouped_ids.update(json.loads(event['items']))
        for row in self.store.db.execute("SELECT id,original,plan FROM organize_items WHERE job_id=? AND state='done' ORDER BY id",(job['id'],)):
            source,plan=json.loads(row['original']),json.loads(row['plan'])
            if plan.get('strm_filtered'):continue
            if json.loads(job['config']).get('job_kind')=='strm_sync' and not plan.get('output_changed'):continue
            relative=plan.get('relative_path','')
            if not relative.endswith('.strm'):continue
            if row['id'] not in reported: paths.append(relative)
            config = json.loads(job['config'])
            if config.get('job_kind', 'organize') in ('organize','share_organize'):
                event_key = f'show_done:{job["id"]}:{relative}:{row["id"]}'
                individual = self.store.db.execute('SELECT telegram_state FROM notifications WHERE event_key=?', (event_key,)).fetchone()
                if row['id'] in grouped_ids or (individual and individual['telegram_state'] != 'failed'):
                    continue
            details=plan.get('details',{})
            if not details:continue
            hint=plan.get('hint',{})
            key=(hint.get('media_type'),details.get('id'),hint.get('season'))
            groups.setdefault(key,[]).append((source,plan))
        config=json.loads(job['config'])
        previous_key = None
        for key,items in groups.items():
            shared=config.get('job_kind')=='share_organize'
            latest=self.store.db.execute('SELECT notify_state FROM organize_jobs WHERE id=?',(job['id'],)).fetchone()
            delivery=self.store.db.execute('SELECT payload FROM emby_deliveries WHERE job_id=?',(job['id'],)).fetchone()
            targets=json.loads(delivery['payload']).get('targets',[]) if delivery else []
            confirmed={t.get('relative') for t in targets if t.get('confirmed')}
            if config.get('notify_emby') and latest and latest['notify_state'] in ('verified','partial','unconfirmed','failed','unknown','not_needed'):
                hits=sum(1 for _,p in items if p.get('relative_path') in confirmed)
                status='verified' if hits==len(items) else ('partial' if hits else 'unconfirmed')
            else:status='pending' if config.get('notify_emby') else ''
            heading,body,photo=media_card(items,shared=shared,transferred=bool(config.get('transfer_parent')),emby_status=status)
            event=f'media:{job["id"]}:{key[0]}:{key[1]}:{key[2]}'
            ids=[r['id'] for r in self.store.db.execute("SELECT id,plan FROM organize_items WHERE job_id=? AND state='done'",(job['id'],))
                 if json.loads(r['plan']).get('relative_path') in {p.get('relative_path') for _,p in items}]
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO media_notice_outbox(job_id) VALUES(?)',(job['id'],))
                self.store.db.execute('INSERT OR IGNORE INTO media_notice_groups(event_key,job_id,items) VALUES(?,?,?)',(event,job['id'],json.dumps(ids)))
            state=await self._publish(event,heading,body,'success',job['id'],photo=photo,depends_on=previous_key)
            previous_key = event
            if photo and state=='failed':
                with self.store.db:self.store.db.execute('UPDATE media_notice_groups SET event_key=? WHERE event_key=?',(event+':text',event))
                previous_key = event + ':text'
                await self._publish(previous_key,heading,body,'success',job['id'])
        changes = self.store.db.execute('''SELECT c.action,c.path FROM strm_changes c WHERE c.job_id=?
            AND NOT EXISTS (SELECT 1 FROM strm_change_items i JOIN strm_reported_items r
              ON r.job_id=i.job_id AND r.item_id=i.item_id WHERE i.job_id=c.job_id AND i.action=c.action AND i.path=c.path)
            ORDER BY c.path''', (job['id'],)).fetchall()
        paths.extend(row['path'] for row in changes if row['action']=='updated')
        removed = sorted({row['path'] for row in changes if row['action']=='removed'}, key=natural_path_key)
        replaced = {row['path'] for row in changes if row['action']=='replaced'}
        pending = sorted({row['path'] for row in changes if row['action']=='remove_pending'} - set(removed), key=natural_path_key)
        if not paths and not removed and not pending:return
        paths = sorted(set(paths), key=natural_path_key)
        config=json.loads(job['config'])
        changed=sum(bool(json.loads(r['plan']).get('output_changed')) for r in self.store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(job['id'],)))
        summary=f'同步视频：{len(paths)} 个\nSTRM 更新：{changed if config.get("job_kind")=="strm_sync" else len(paths)} 个\n清理 STRM：{len(removed)} 个文件'
        if replaced: summary += f'\n同路径覆盖：{len(replaced)} 个 STRM'
        if pending: summary += f'\n清理结果待核实：{len(pending)} 个 STRM'
        scope = f'任务 #{job["id"]}'
        if config.get('job_kind') != 'strm_sync':
            event = f'strm:{job["id"]}:summary'
            await self._publish(event,f'📦 {strm_source(config)} · STRM 批次结果',scope+'\n'+summary,
                                job_id=job['id'],depends_on=previous_key)
            previous_key = event
        # 同步任务使用统一结果；整理任务继续保留批次汇总和明细。
        # 明细只留给整理触发的增量（见 _group_strm）。
        if config.get('job_kind') != 'strm_sync':
            expanded = sidecar_paths(paths, strm_root())
            count = strm_entry_count(expanded)
            title_label = 'STRM 与 NFO 文件明细' if count != len(expanded) else 'STRM 文件明细'
            chunks=strm_tree(expanded)
            for i,(chunk,title) in enumerate(zip(chunks,strm_detail_titles(chunks,count,title_label,strm_source(config))),1):
                event = f'strm:{job["id"]}:files:{i}'
                await self._publish(event,title,scope+'\n🔗 新增或更新\n'+chunk,job_id=job['id'],depends_on=previous_key)
                previous_key = event
        chunks=strm_tree(removed)
        for i,(chunk,title) in enumerate(zip(chunks,strm_detail_titles(chunks,len(removed),source=strm_source(config))),1):
            event = f'strm:{job["id"]}:files:removed:{i}'
            await self._publish(event,title,scope+'\n🧹 已清理旧 STRM\n'+chunk,job_id=job['id'],depends_on=previous_key)
            previous_key = event
        chunks=strm_tree(pending)
        for i,(chunk,title) in enumerate(zip(chunks,strm_detail_titles(chunks,len(pending),source=strm_source(config))),1):
            event = f'strm:{job["id"]}:files:pending:{i}'
            await self._publish(event,title,scope+'\n⚠️ 清理结果待核实\n'+chunk,level='warning',
                                job_id=job['id'],depends_on=previous_key)
            previous_key = event
