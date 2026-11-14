"""Explicit, scoped 115 metadata-only work cleanup with revalidation and durable intents."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

from .cloud115 import CloudError, fatal, unescape_115_charref
from .inventory_orphans import MEDIA_DIR, ProtectedContent, inventory
from .media import KNOWN_VIDEOS, METADATA, SUBTITLES
from .recycle import match_record

ALLOWED = METADATA | SUBTITLES | {'.json', '.bif'}
MAX_CANDIDATES = 1000
MAX_ITEMS = 1000


def normalized(entry, parent):
    fid, name = str(entry.get('id') or ''), entry.get('name')
    if (not fid.isdigit() or not isinstance(name, str) or not name or '/' in name
            or '\\' in name or name in ('.', '..') or not isinstance(entry.get('is_dir'), bool)):
        raise ValueError('网盘条目身份不完整')
    if str(entry.get('parent_id', parent)) != str(parent):
        raise ValueError('网盘条目父目录不一致')
    try:
        size = int(entry.get('size') or 0)
    except (TypeError, ValueError):
        raise ValueError('网盘条目大小无效') from None
    if size < 0: raise ValueError('网盘条目大小无效')
    return {'id':fid, 'name':name, 'parent_id':str(parent), 'is_dir':entry['is_dir'],
            'size':0 if entry['is_dir'] else size, 'sha1':str(entry.get('sha1') or '').upper()}


def digest(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


async def inspect(cloud, archive, cid):
    trail = await cloud.path_info(cid)
    ids = [str(item['id']) for item in trail]
    if (not archive.isdigit() or archive == '0' or cid == archive or len(ids) != len(set(ids))
            or archive not in ids or not ids or ids[-1] != cid or not MEDIA_DIR.search(trail[-1]['name'])):
        raise ValueError('目录不属于归档库中的 TMDB 作品目录')
    root = {'id':cid, 'name':trail[-1]['name'], 'parent_id':ids[-2], 'is_dir':True, 'size':0, 'sha1':''}
    entries = {cid:root}; paths = {cid:trail}; stack = [cid]
    while stack:
        parent = stack.pop()
        children = await cloud.children_verified(parent)
        for raw in children:
            entry = normalized(raw, parent); fid = entry['id']
            if fid in entries: raise ValueError('网盘目录存在重复或循环')
            if not entry['is_dir'] and Path(entry['name']).suffix.lower() not in ALLOWED:
                raise ValueError('含视频或未知文件，已保留')
            entries[fid] = entry
            if len(entries) > MAX_ITEMS: raise ValueError('目录内容过多，已保留')
            if entry['is_dir']:
                paths[fid] = paths[parent] + [{'id':fid, 'name':entry['name']}]
                if len(paths[fid]) > 80: raise ValueError('目录过深，已保留')
                stack.append(fid)
    snapshot = {'archive':archive, 'trail':trail, 'entries':sorted(entries.values(), key=lambda x:x['id'])}
    return snapshot, paths


def local_relative(snapshot, library):
    if not library or library in ('.', '..') or '/' in library or '\\' in library:
        raise ValueError('本地媒体库根目录名称无效')
    trail = snapshot['trail']
    ids = [str(row['id']) for row in trail]
    names = [row['name'] for row in trail[ids.index(snapshot['archive']) + 1:]]
    if any(not name or name in ('.', '..') or '/' in name or '\\' in name for name in names):
        raise ValueError('目录名称无法安全映射到本地')
    return '/'.join([library, *names])


def verify_local(engine, relative, library):
    root = Path(engine.root)
    if root.is_symlink() or not root.is_dir(): raise ValueError('本地 STRM 根目录不可用')
    path = root
    for part in relative.split('/'):
        path = path / part
        if path.is_symlink(): raise ValueError('本地对应路径包含软链接')
    for row in engine.store.db.execute('SELECT relative_path FROM organized_files'):
        if row[0].startswith(relative.rstrip('/') + '/'):
            raise ValueError('本地对应目录仍有网盘源文件登记，已保留')
    if path.exists():
        # Reject any local media, unknown entry or symlink, even when it is unregistered.
        inventory(root, relative, [library])


async def scan(engine, archive, account, library):
    cloud = engine.cloud
    archive = str(archive or '')
    if not archive.isdigit() or archive == '0': raise ValueError('请先配置影视整理的网盘归档目录')
    dirs, files = await cloud.download_tree(archive)
    nodes = {str(row['id']):row for row in dirs}
    if len(nodes) != len(dirs) or archive in nodes: raise ValueError('网盘目录清单不完整')
    roots = set()
    for cid, row in nodes.items():
        if MEDIA_DIR.search(unescape_115_charref(str(row.get('name') or ''))):
            roots.add(cid)
    if len(roots) > 20000: raise ValueError('作品目录过多，请缩小网盘归档范围')
    def owner(cid):
        seen=set(); found=None
        while cid != archive:
            if cid in seen or cid not in nodes: raise ValueError('网盘目录层级不完整')
            seen.add(cid)
            if cid in roots: found=cid
            cid=str(nodes[cid].get('parent_id') or '')
        return found
    for cid in nodes: owner(cid)
    def folder_key(cid):
        parts=[]
        while cid != archive:
            row=nodes[cid]; name=unescape_115_charref(str(row.get('name') or ''))
            if not name or name in ('.', '..') or '/' in name or '\\' in name:
                raise ValueError('网盘目录名称无效')
            parts.append(name); cid=str(row['parent_id'])
        return tuple(reversed(parts))
    root_paths = {}
    for cid in roots:
        if owner(cid) == cid:
            root_paths.setdefault(folder_key(cid), []).append(cid)
    ambiguous = {cid for values in root_paths.values() if len(values) > 1 for cid in values}
    blocked=set(); unknown={}
    for item in files:
        candidate=owner(str(item.get('parent_id') or ''))
        name = str(item.get('name') or '')
        suffix = Path(name).suffix.lower()
        if candidate and suffix not in ALLOWED:
            blocked.add(candidate)
            if suffix not in KNOWN_VIDEOS:
                unknown.setdefault(candidate, name)
    # Only outermost work directories; inner folders are included in their parent.
    candidates=[cid for cid in roots if owner(cid)==cid and cid not in blocked and cid not in ambiguous]
    if len(candidates) > MAX_CANDIDATES: raise ValueError('候选超过 1000 个，请缩小网盘归档范围')
    items=[]; skipped=[]
    attention = [{'id':cid, 'path':' / '.join(folder_key(cid)), 'reason':'存在未知文件：'+name[:160]}
                 for cid,name in unknown.items()]
    attention += [{'id':cid, 'path':' / '.join(folder_key(cid)), 'reason':'存在同名作品目录，无法确定对应关系'}
                  for cid in ambiguous]
    for cid in sorted(candidates):
        try:
            snapshot, _ = await inspect(cloud, archive, cid)
            relative = local_relative(snapshot, library)
            verify_local(engine, relative, library)
            entries=snapshot['entries']; trail=snapshot['trail']
            items.append({'id':cid, 'path':' / '.join(x['name'] for x in trail if x['name']),
                          'local_path':relative, 'fingerprint':digest({**snapshot, 'account':account, 'library':library}), 'files':sum(not x['is_dir'] for x in entries),
                          'dirs':sum(x['is_dir'] for x in entries),
                          'file_names':[x['name'] for x in entries if not x['is_dir']][:100]})
        except (ValueError, CloudError, OSError) as exc:
            if fatal(exc): raise
            skipped.append({'id':cid, 'reason':str(exc)[:180]})
            if not ((isinstance(exc, ProtectedContent) and exc.ordinary)
                    or str(exc) == '本地对应目录仍有网盘源文件登记，已保留'):
                attention.append({'id':cid, 'path':' / '.join(folder_key(cid)), 'reason':str(exc)[:180]})
    return {'items':items, 'skipped':len(blocked | ambiguous)+len(skipped), 'skipped_items':skipped[:100],
            'attention_count':len(attention), 'attention_items':attention[:200]}


async def confirm(cloud, source, intent):
    for attempt in range(3):
        record = match_record(source, intent, await cloud.recycle_files())
        if record is not None and not await cloud.live_file_exists(source['id']):
            return record
        if attempt < 2: await asyncio.sleep(.35 * (attempt + 1))
    raise ValueError('回收站结果未能确认；已停止，不能重复提交')


async def clean_one(engine, archive, library, cid, expected, progress=None):
    cloud, store = engine.cloud, engine.store
    snapshot, paths = await inspect(cloud, archive, cid)
    account = engine.index.account({})
    if digest({**snapshot, 'account':account, 'library':library}) != expected: raise ValueError('目录内容、账号或库根已变化，请重新扫描')
    relative = local_relative(snapshot, library)
    verify_local(engine, relative, library)
    entries = {e['id']:e for e in snapshot['entries']}
    remaining = {d: {e['id']:e for e in entries.values() if e['id'] != cid and e['parent_id'] == d}
                 for d,e in entries.items() if e['is_dir']}
    files = [e for e in entries.values() if not e['is_dir']]
    directories = sorted((e for e in entries.values() if e['is_dir']), key=lambda e:len(paths[e['id']]), reverse=True)
    result={'id':cid, 'path':' / '.join(x['name'] for x in snapshot['trail'] if x['name']),
            'files_removed':0, 'dirs_removed':0, 'errors':[], 'entries':[]}
    if progress:
        progress({'path':result['path'], 'files_total':len(files), 'dirs_total':len(directories),
                  'files_done':0, 'dirs_done':0, 'current':''})
    for source in files + directories:
        fid, parent = source['id'], source['parent_id']
        try:
            if progress: progress({'current':source['name']})
            engine.index.account({'account_id':account})
            verify_local(engine, relative, library)
            # Every operation rechecks the work's position and exact current parent contents.
            if await cloud.path_info(cid) != snapshot['trail']: raise ValueError('作品目录位置变化')
            target_parent = fid if source['is_dir'] else parent
            if await cloud.path_info(target_parent) != paths[target_parent]: raise ValueError('目录路径发生变化')
            if source['is_dir']:
                if remaining[fid] or await cloud.children_verified(fid): raise ValueError('目录不为空或内容变化')
            else:
                actual = await cloud.file(fid)
                if any(str(actual.get(k)) != str(source[k]) for k in ('id','name','parent_id','size')):
                    raise ValueError('附属文件身份或位置变化')
                if source['sha1'] and str(actual.get('sha1') or '').upper() != source['sha1']:
                    raise ValueError('附属文件内容变化')
            if parent in remaining:
                observed = {e['id']:e for e in (normalized(x,parent) for x in await cloud.children_verified(parent))}
                if observed != remaining[parent]: raise ValueError('父目录内容变化')
            key = 'cloud-orphan-intent:' + account + ':' + fid
            if store.meta(key): raise ValueError('该对象已有回收记录，不能重复提交')
            before = await cloud.recycle_files()
            intent={'source':source, 'state':'intent', 'at':time.time(), 'before_rids':[e['rid'] for e in before]}
            store.set_meta(key, intent)
            if source['is_dir']:
                if hasattr(cloud, 'forget_children'): cloud.forget_children([fid, parent])
                await cloud.recycle_empty_directory(fid, parent)
            else:
                await cloud.recycle(fid, parent)
            record = await confirm(cloud, source, intent)
            store.set_meta(key, {**intent, 'state':'confirmed', 'rid':record['rid']})
            if parent in remaining: remaining[parent].pop(fid, None)
            result['dirs_removed' if source['is_dir'] else 'files_removed'] += 1
            parent_path = paths.get(parent, snapshot['trail'][:-1])
            result['entries'].append({'path':' / '.join([x['name'] for x in parent_path if x['name']] + [source['name']]),
                                      'kind':'目录' if source['is_dir'] else '文件'})
            if progress: progress({'files_done':result['files_removed'], 'dirs_done':result['dirs_removed'],
                                   'entries':result['entries']})
        except (CloudError, ValueError, OSError) as exc:
            result['errors'].append(str(exc)[:200]); break
    return result
