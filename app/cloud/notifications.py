"""Stage notifications; Emby receipts are web-only, not another Telegram card."""
import hashlib
import html
import json
import re
import time
from pathlib import PurePosixPath
from app.cloud import store, settings, events
from app.domain import media_labels


def group_key(asset, connection, revision, origin):
    # Keep existing ingestion identities stable across upgrades.
    m = asset['payload']['media']
    return hashlib.sha256(store.encode([origin or asset['id'], connection, revision, m['kind'], str(m['tmdb_id']), str(m.get('season', '')), asset['payload'].get('source_kind', 'personal')]).encode()).hexdigest()


def deliver(job_id, key, sender):
    """Claim before sending. Ambiguous sends must never be replayed."""
    with store.db() as con:
        old = con.execute('SELECT state FROM cloud_steps WHERE job_id=? AND step_key=?', (job_id, key)).fetchone()
        if old:
            return old[0] == 'verified'
        con.execute('INSERT INTO cloud_steps VALUES(?,?,?,?,?,?)', (job_id, key, 'dispatched', '{}', '{}', time.time()))
    sent = False
    try:
        sent = bool(sender())
    except Exception:
        # Transport errors may contain bot credentials; never log raw exceptions.
        pass
    with store.db() as con:
        con.execute('UPDATE cloud_steps SET state=?,result=?,updated=? WHERE job_id=? AND step_key=?', ('verified' if sent else 'unknown', store.encode({'sent': sent}), time.time(), job_id, key))
    label = '整理海报' if key.startswith('poster:') else '任务通知'
    events.emit('notify', label + ('已发送' if sent else '发送失败或结果未知，未自动重发'), job_id, level='INFO' if sent else 'WARNING', details={'event': 'poster' if key.startswith('poster:') else 'result', 'status': 'sent' if sent else 'unknown'})
    return sent


def work_key(media):
    return (media['kind'], str(media['tmdb_id']), str(media.get('season', '')) if media['kind'] == 'tv' else '')


def caption(items, source_kind='personal', expected=None):
    m = items[0]['media']
    def safe(value):
        return html.escape(str(value))
    episodes = sorted({int(i['media']['episode']) for i in items if i['media'].get('episode') is not None})
    ranges = []
    for n in episodes:
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    suffix = ''
    if m['kind'] == 'tv':
        suffix = ' S' + str(int(m['season'])).zfill(2) + ' ' + ', '.join('E' + str(a).zfill(2) + (('-E' + str(b).zfill(2)) if b != a else '') for a, b in ranges[:6]) + (' 等 '+str(len(episodes))+' 集' if len(ranges)>6 else '')
    versions = sorted({media_labels.version(i['media'], i['source']) for i in items})
    groups = sorted({g for i in items if (g := media_labels.release_group(i['media']))})
    def short_list(values, count=3):
        return ' / '.join(values[:count]) + (' 等 '+str(len(values))+' 种' if len(values)>count else '')
    size = sum(int(i['source'].get('size') or 0) for i in items)
    parts = PurePosixPath(m['directory']).parts
    if source_kind == 'share' and parts and parts[0].startswith('分享-'):
        parts = parts[1:]
    category = '/'.join(parts[:-2] if m['kind'] == 'tv' else parts[:-1]) or '未分类'
    heading = '分享 STRM 已生成' if source_kind == 'share' else '115 整理完成'
    text = '✅ ' + heading + '\n📺 ' + safe(str(m['title'])[:120]) + (' (' + safe(m.get('year')) + ')' if str(m.get('year', '')) not in ('', '0', '0000', 'None') else '') + safe(suffix)
    text += '\n☁️ 来源：' + ('115网盘分享（免转存）' if source_kind == 'share' else '115网盘')
    text += '\n🗂 分类：' + safe(category[:80]) + '\n🎞 版本：' + safe(short_list(versions))
    text += '\n📁 文件：' + str(len(items)) + ' 个' + (('\n🎬 本次：' + str(len(episodes)) + ' 集') if episodes else '')
    if m['kind'] == 'tv':
        totals = {i['media'].get('season_episode_count') for i in items if type(i['media'].get('season_episode_count')) is int}
        text += '\n🗓 本季：S' + str(int(m['season'])).zfill(2) + (' 共 ' + str(next(iter(totals))) + ' 集（TMDB）' if len(totals) == 1 else ' 总集数暂未取得')
    text += '\n💾 体积：' + format(size / 1024**3, '.2f') + ' GB\n🎟 影号：TMDB ' + safe(m['tmdb_id'])
    if groups:
        text += '\n👥 组名：' + safe(short_list(groups))
    if expected and expected > len(items):
        text += '\n⚠️ 本组仅完成 ' + str(len(items)) + '/' + str(expected) + ' 个文件，其余请查看任务记录。'
    return text


def poster_owner(job_id):
    """Recovery keeps the original work/season claim, including unknown sends."""
    seen = set()
    with store.db() as con:
        while job_id not in seen and len(seen) < 64:
            seen.add(job_id)
            row = con.execute('SELECT payload FROM cloud_jobs WHERE id=?',(job_id,)).fetchone()
            parent = json.loads(row[0]).get('recovery_of') if row else None
            if not parent:
                return job_id
            job_id = parent
    from app.cloud.errors import CloudError
    raise CloudError('恢复通知关联异常，保留原通知回执',409)


class BatchPosters:
    """One card per work/season, containing only verified operations.

    Publishers call verified only after the STRM write and binding receipt.
    Group readiness counts all terminal items, independent of execution order.
    """
    def __init__(self, job_id, planned, source_kind='personal'):
        self.job_id, self.source_kind = job_id, source_kind
        self.groups, self.last, self.expected = {}, {}, {}
        self.work_at_index = {}
        self.done, self.finished = {}, set()
        for index, item in enumerate(planned):
            if item.get('unknown'):
                continue
            key = work_key(item['media'])
            self.work_at_index[index] = key
            self.last[key] = index
            self.expected[key] = self.expected.get(key, 0) + 1

    def verified(self, index, source, media):
        key = work_key(media)
        self.groups.setdefault(key, []).append({'source': source, 'media': media})
        self.finish(index)

    def finish(self, index):
        # Include the successful subset if the group's last file failed.
        key = self.work_at_index.get(index)
        if key is not None and index not in self.finished:
            self.finished.add(index)
            self.done[key] = self.done.get(key, 0) + 1
            if self.done[key] == self.expected[key]:
                self._send(key)

    def flush(self):
        for key in list(self.groups):
            self._send(key)

    def _send(self, key):
        items = self.groups.pop(key, [])
        if not items:
            return
        cfg = settings.get()
        module = 'notify_strm' if self.source_kind == 'share' else 'notify_organize'
        if not cfg['notify_enabled'] or not cfg[module]:
            events.emit('notify', '海报未发送：' + ('notify_enabled' if not cfg['notify_enabled'] else module) + ' 已关闭', self.job_id)
            return
        from app.tg import notify_telegram_card
        poster = items[0]['media'].get('backdrop_path') or items[0]['media'].get('poster_path')
        image = 'https://image.tmdb.org/t/p/w500' + poster if poster else ''
        claim = 'poster:' + hashlib.sha256(store.encode([self.source_kind, *key]).encode()).hexdigest()
        deliver(poster_owner(self.job_id), claim, lambda: notify_telegram_card(caption(items, self.source_kind, self.expected[key]), image))


def result_files(generated):
    """Resolve receipts once, with a fallback for older path-only job results."""
    rows, seen = [], set()
    with store.db() as con:
        for value in generated:
            path = value.get('path', '') if isinstance(value, dict) else str(value)
            if not path or path in seen:
                continue
            seen.add(path)
            aid = value.get('asset_id') if isinstance(value,dict) else None
            record = con.execute("SELECT payload FROM cloud_assets WHERE id=? AND local_path=? AND state='active'",(aid,path)).fetchone() if aid else con.execute("SELECT payload FROM cloud_assets WHERE local_path=? AND state='active'",(path,)).fetchone()
            payload = json.loads(record[0]) if record else {}
            media = payload.get('media',{})
            parent = str(PurePosixPath(path).parent)
            if payload:
                try:
                    parent = str(PurePosixPath(parent).relative_to(payload['output_root']))
                except ValueError:
                    pass
            rows.append({'path':path, 'directory':parent, 'name':PurePosixPath(path).name,
                         'media':media, 'owned':payload.get('owned',[])})
    def natural(value):
        return tuple((0,int(p)) if p.isdecimal() else (1,p.casefold()) for p in re.split(r'(\d+)',value))
    return sorted(rows,key=lambda r:(natural(r['directory']), r['media'].get('season') if type(r['media'].get('season')) is int else -1, r['media'].get('episode') if type(r['media'].get('episode')) is int else -1, natural(r['name'])))


def batch_result(operation, state, message, job_id='', kind='', diagnostic_report=None):
    from app.cloud import diagnostics
    rows = result_files(operation.get('generated', []))
    directories = operation.get('publication') or {r['directory']:{'published':1,'failed':0} for r in rows}
    success = sum(v.get('published',0)>0 and not v.get('failed') for v in directories.values())
    failed = sum(bool(v.get('failed')) for v in directories.values())
    metadata = {v['path'] for r in rows for v in r['owned'] if v.get('path')}
    title = 'STRM 批量结果' if rows or directories or kind in ('sync', 'publish', 'share_strm') else '整理结果'
    text = '📦 '+title+'：' + html.escape(events.clean(message)[:160])
    if directories:
        text += '\n同步目录：'+str(len(directories))+' 个 · 目录成功：'+str(success)+' 个'
        if failed:
            text += ' · 目录异常：'+str(failed)+' 个'
    text += '\nSTRM 更新：'+str(len(rows))+' 个'
    if metadata:
        text += ' · 元数据写入：'+str(len(metadata))+' 个'
    for key, label in (('errors','处理异常'), ('skipped','跳过'), ('filtered','规则过滤'), ('quality_held','版本保留')):
        if operation.get(key):
            text += '\n'+label+'：'+str(len(operation[key]))+' 项'
    removed = operation.get('reconciliation',{}).get('removed',[])
    if removed:
        text += '\n失效引用清理：'+str(len(removed))+' 项'
    if operation.get('cleanup', {}).get('removed'):
        text += '\n来源空目录清理：'+str(len(operation['cleanup'].get('removed',[])))+' 个'
    if operation.get('deferred'):
        text += '\n等待延时生成：'+str(len(operation['deferred']))+' 项'
    if state == 'stopped':
        text += '\n任务已停止，仅统计已完成项目。'
    problems = diagnostic_report or diagnostics.report(operation, state, message, limit=3)
    shown = 0
    for item in problems['items'][:3]:
        chunk = '\n\n'+html.escape(item['label']+'：'+item['name'][:90])
        chunk += '\n原因：'+html.escape(item['reason'][:180])
        if item['rule']:
            chunk += '\n规则：'+html.escape(item['rule'][:100])
        chunk += '\n处理：'+html.escape(item['suggestion'][:120])
        if len((text+chunk).encode('utf-16-le'))//2 > 3300:
            break
        text += chunk
        shown += 1
    if job_id:
        text += '\n\n任务：'+html.escape(job_id[:12])+'（网页任务中心可搜索此编号）'
    if problems['total'] > shown:
        text += '\n其余 '+str(problems['total']-shown)+' 项在任务详情的处理清单中查看。'
    return text, rows


def detail_batches(rows):
    def units(text):
        return len(html.escape(text).encode('utf-16-le')) // 2
    entries, seen = [], set()
    for row in rows:
        entries.append((row['directory'], row['name'], '🔗'))
        for owned in sorted(row['owned'], key=lambda v:v.get('path','')):
            path = owned.get('path')
            if path and path not in seen:
                seen.add(path)
                entries.append((row['directory'],PurePosixPath(path).name,'⬇️'))
    # Account for both 20-file batching and Telegram's message length. A long
    # filename remains intact; directories are repeated on the next message.
    chunks, chunk, length, previous = [], [], 0, None
    for directory, name, mark in entries:
        cost = units(name) + (units(directory)+8 if directory != previous else 0) + 10
        if chunk and (len(chunk)>=20 or length+cost>3400):
            chunks.append(chunk);chunk=[];length=0;previous=None
            cost = units(name)+units(directory)+18
        chunk.append((directory,name,mark));length+=cost;previous=directory
    if chunk:
        chunks.append(chunk)
    messages, offset = [], 0
    for index, chunk in enumerate(chunks,1):
        text = '📄 STRM 文件明细 '+str(index)+'/'+str(len(chunks))+'（'+str(offset+1)+'–'+str(offset+len(chunk))+'/'+str(len(entries))+'）'
        previous = None
        for directory, name, mark in chunk:
            if directory != previous:
                text += '\n\n📂 '+html.escape(directory)
                previous=directory
            text += '\n  '+mark+' '+html.escape(name)
        messages.append(text);offset+=len(chunk)
    return messages


def deferred_result(job_id):
    """Aggregate delayed child receipts once all children reach a terminal state."""
    with store.db() as con:
        parent = con.execute('SELECT origin_job FROM cloud_job_due WHERE job_id=?',(job_id,)).fetchone()
        if not parent:
            return None
        origin = parent[0]
        jobs = con.execute('SELECT j.state,j.payload,j.result FROM cloud_job_due d JOIN cloud_jobs j ON j.id=d.job_id WHERE d.origin_job=? ORDER BY j.created,j.id',(origin,)).fetchall()
        if any(row[0] in ('queued','running') for row in jobs):
            return None
    planned, result = [], {'generated':[],'errors':[],'publication':{}}
    ready = []
    for index, (state, raw, output) in enumerate(jobs):
        payload, receipt = json.loads(raw), json.loads(output)
        planned.append({'media':payload['media']})
        generated = receipt.get('generated',[])
        result['generated'].extend(generated)
        errors = receipt.get('errors',[])
        result['errors'].extend(errors)
        if state in ('failed','uncertain','needs_review','stopped') and not errors:
            result['errors'].append({'file_id':payload['source']['id'],'reason':'延时发布未完成，请查看子任务'})
        counts=result['publication'].setdefault(payload['media']['directory'],{'published':0,'failed':0})
        counts['published']+=len(generated)
        counts['failed']+=int(not generated)
        if generated:
            ready.append((index,payload))
    posters=BatchPosters(origin,planned)
    for index,payload in ready:
        posters.verified(index,payload['source'],payload['media'])
    for index in range(len(planned)):
        posters.finish(index)
    posters.flush()
    return origin, result
