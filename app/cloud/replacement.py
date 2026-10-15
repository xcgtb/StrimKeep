"""Opt-in quality selection and source-bound upgrades after new STRM publication."""

import copy
import json
import re
import threading
import time
from pathlib import Path, PurePosixPath
from app.cloud import store, settings, quality8, strm, governance, jobs
from app.cloud.errors import CloudError

_LOCK = threading.RLock()
BUCKETS = {'DV': '杜比', 'non-DV': '非杜比', 'unknown': '动态范围未知'}


def enabled(config):
    return config['quality8_enabled'] and config['quality8_mode'] == 'upgrade'


def decorate(row, media, config, previous=None):
    suffix = (previous or {}).get('quality_suffix', '')
    if not suffix and not previous and enabled(config) and config['quality8_coexist_dv']:
        suffix = BUCKETS[quality8.bucket(quality8.facts(row, media))]
    if suffix:
        parts = PurePosixPath(suffix).parts
        if any(p in ('/', '.', '..') for p in parts) or '\\' in suffix:
            raise CloudError('版本归档路径无效', 409)
        media['directory'] = str(PurePosixPath(media['directory']) / suffix)
        media['quality_suffix'] = suffix
    return media


def within(provider, row, scope):
    parent, seen = row['parent_id'], set()
    while parent != scope:
        if parent == '0' or parent in seen or len(seen) >= 64:
            raise CloudError('已有版本不在本次归档范围', 409)
        seen.add(parent)
        folder = provider.info(parent)
        if not folder['is_dir']:
            raise CloudError('归档目录身份异常', 409)
        parent = folder['parent_id']


def bound_asset(asset, provider, scope):
    from app.cloud.workflow import _snapshot_equal
    if asset['state'] != 'active' or Path(asset['payload']['output_root']) != strm.root():
        raise CloudError('已有版本尚未就绪或属于其他输出根目录', 409)
    row = asset['payload']['source']
    if not _snapshot_equal(row, provider.info(row['id'])):
        raise CloudError('已有版本来源身份或位置变化', 409)
    within(provider, row, scope)
    bound = governance.lookup(asset['local_path'], strm.root())
    return {'asset': asset, 'bound': bound}


def validate_refs(refs, provider):
    selected = {r['bound']['path'] for r in refs}
    selected_assets = {r['asset']['id'] for r in refs}
    # A subtitle can be registered as another asset's companion, not only as file_id.
    ids = {s['id'] for r in refs for s in r['bound']['sources']}
    if refs:
        account_id = refs[0]['bound']['account_id']
        with store.db() as con:
            others = con.execute("SELECT id,payload FROM cloud_assets WHERE account_id=? AND coalesce(json_extract(payload,'$.source_kind'),'personal')='personal' AND state IN ('active','publishing','needs_review')", (account_id,)).fetchall()
        for aid, raw in others:
            if aid in selected_assets:
                continue
            payload = json.loads(raw)
            if ids & {s['id'] for s in [payload['source'], *payload.get('companions', [])]}:
                raise CloudError('旧源或关联文件仍有其他播放引用，保留已有版本', 409)
    for ref in refs:
        current_asset = store.asset(ref['asset']['id'])
        if not current_asset or current_asset['state'] != 'active':
            raise CloudError('旧引用状态在预览后变化，保留旧源', 409)
        if governance.lookup(ref['bound']['path'], strm.root()) != ref['bound']:
            raise CloudError('旧版本本地身份在预览后变化', 409)
        within(provider, ref['asset']['payload']['source'], ref['asset']['payload']['scope_cid'])
        governance.delete_sources(ref['bound'], Path(ref['asset']['payload']['output_root']), selected, dry_run=True)


def reserve(provider, scope, row, media, account_id):
    """A colliding new version gets a separate path; the old one is never renamed away."""
    parent, collision = scope, False
    for segment in PurePosixPath(media['directory']).parts:
        matches = [r for r in provider.list(parent) if r['name'].casefold() == segment.casefold()]
        if not matches:
            parent = None
            break
        if len(matches) != 1 or not matches[0]['is_dir']:
            raise CloudError('版本目录有同名冲突', 409)
        parent = matches[0]['id']
    if parent:
        collision = any(r['name'].casefold() == media['name'].casefold() and r['id'] != row['id'] for r in provider.list(parent))
    relative = str(PurePosixPath(media['directory']) / PurePosixPath(media['name']).with_suffix('.strm'))
    local = strm.target(strm.root(), relative)
    with store.db() as con:
        own = con.execute('SELECT account_id,file_id FROM cloud_assets WHERE local_path=?', (str(local),)).fetchone()
    if collision or local.exists() and (not own or own[0] != account_id or own[1] != row['id']):
        suffix = '版本-'+row['id']
        media['directory'] = str(PurePosixPath(media['directory']) / suffix)
        media['quality_suffix'] = str(PurePosixPath(media.get('quality_suffix', '')) / suffix)


def select(provider, account, scope, items, config, job_id=None):
    """Select a batch winner per media/bucket, retain losers at their original source."""
    groups, held, selected = {}, [], []
    def key(row, media):
        bucket = quality8.bucket(quality8.facts(row, media)) if config['quality8_coexist_dv'] else 'all'
        return quality8.identity(media), bucket
    def hold(item, reason, blocked=False):
        held.append({'file_id': item['source']['id'], 'name': item['source']['name'], 'reason': reason, 'blocked': blocked})
    for item in items:
        groups.setdefault(key(item['source'], item['media']), []).append(item)
    with store.db() as con:
        records = con.execute("SELECT id FROM cloud_assets WHERE account_id=? AND coalesce(json_extract(payload,'$.source_kind'),'personal')='personal' AND state IN ('active','publishing','needs_review') AND json_extract(payload,'$.scope_cid')=?", (account['id'], scope)).fetchall()
        pending = con.execute("SELECT payload FROM cloud_jobs WHERE kind='publish' AND state IN ('queued','running','uncertain','needs_review') AND json_extract(payload,'$.account_id')=?", (account['id'],)).fetchall()
    assets = [store.asset(r[0]) for r in records]
    existing_groups = {}
    for asset in assets:
        existing_groups.setdefault(key(asset['payload']['source'], asset['payload']['media']), []).append(asset)
    pending_keys = {key(p['source'], p['media']) for raw, in pending if (p := json.loads(raw)).get('scope_cid') == scope}
    paths = set()
    for identity, candidates in groups.items():
        jobs.checkpoint(job_id)
        winner = candidates[0]
        for item in candidates[1:]:
            comparison = quality8.compare(quality8.facts(item['source'], item['media']), quality8.facts(winner['source'], winner['media']), config['quality8_rules'])
            if comparison['result'] == 1:
                winner = item
        for item in candidates:
            if item is not winner:
                hold(item, '本批已有更优或平局版本，保留来源原位')
        old = [a for a in existing_groups.get(identity, []) if a['file_id'] != winner['source']['id']]
        try:
            if identity in pending_keys:
                raise CloudError('同作品版本仍有待核实或延时发布任务，请完成后重新预览', 409)
            refs = [bound_asset(a, provider, scope) for a in old]
            if refs:
                if winner['source']['size'] <= 0:
                    raise CloudError('新视频大小无效，不能自动洗版', 409)
                if not re.fullmatch(r'[A-Fa-f0-9]{40}', winner['source']['sha1'] or ''):
                    raise CloudError('新源缺少完整内容指纹，不能自动洗版', 409)
                if any(quality8.compare(quality8.facts(winner['source'], winner['media']), quality8.facts(r['asset']['payload']['source'], r['asset']['payload']['media']), config['quality8_rules'])['result'] <= 0 for r in refs):
                    hold(winner, '未优于已有版本（平局保留已有），保留来源原位')
                    continue
                validate_refs(refs, provider)
            for ref in refs:
                ref['rules'] = copy.deepcopy(config['quality8_rules'])
                ref['coexist'] = config['quality8_coexist_dv']
            reserve(provider, scope, winner['source'], winner['media'], account['id'])
            path = (winner['media']['directory']+'/'+str(PurePosixPath(winner['media']['name']).with_suffix('.strm'))).casefold()
            if path in paths:
                raise CloudError('不同作品将生成相同 STRM，请修正命名模板', 409)
            paths.add(path)
            winner['upgrades'] = refs
            selected.append(winner)
        except CloudError as error:
            hold(winner, str(error), True)
    return selected, held


def _save(record, state, result):
    with store.db() as con:
        con.execute('UPDATE cloud_upgrades SET state=?,result=?,updated=? WHERE id=?', (state, store.encode(result), time.time(), record['id']))


def _records(job_id):
    with store.db() as con:
        rows = con.execute('SELECT id,new_asset,old_asset,state,payload,result FROM cloud_upgrades WHERE job_id=? ORDER BY updated,id', (job_id,)).fetchall()
    return [dict(zip(('id','new_asset','old_asset','state','payload','result'), (r[0],r[1],r[2],r[3],json.loads(r[4]),json.loads(r[5])))) for r in rows]


def summary(job_id):
    return [{'id': r['id'], 'new_asset': r['new_asset'], 'old_asset': r['old_asset'], 'state': r['state'], **r['result']} for r in _records(job_id)]


def check_new(asset_id, record):
    from app.cloud import workflow
    config = settings.get()
    if not enabled(config) or config['quality8_rules'] != record['rules'] or config['quality8_coexist_dv'] != record['coexist']:
        raise CloudError('洗版策略已关闭或变化，保留旧版本；核对原策略后再继续', 409)
    asset = store.asset(asset_id)
    if not asset or asset['state'] != 'active' or asset['payload'].get('warnings'):
        raise CloudError('新版本 STRM 或关联输出尚未完整就绪，保留旧版本', 409)
    bound = governance.lookup(asset['local_path'], strm.root())
    if bound['account_id'] != record['bound']['account_id'] or bound['account_revision'] != record['bound']['account_revision']:
        raise CloudError('洗版账号或授权版本已变化', 409)
    provider = workflow.verify_account({'account_id': bound['account_id'], 'account_revision': bound['account_revision']})
    for source in bound['sources']:
        if not workflow._snapshot_equal(source, provider.info(source['id'])):
            raise CloudError('新版本来源变化，保留旧版本', 409)
    if not workflow._snapshot_equal(asset['payload']['source'], record['replacement_source']):
        raise CloudError('新版本与洗版登记快照不同，保留旧版本', 409)
    old_media = record['asset']['payload']['media']
    if quality8.identity(asset['payload']['media']) != quality8.identity(old_media):
        raise CloudError('新旧版本作品身份不一致，保留旧版本', 409)
    newer = quality8.facts(asset['payload']['source'], asset['payload']['media'])
    older = quality8.facts(record['asset']['payload']['source'], old_media)
    if record['coexist'] and quality8.bucket(newer) != quality8.bucket(older) or quality8.compare(newer, older, record['rules'])['result'] <= 0:
        raise CloudError('当前新版本已不满足原洗版策略，保留旧版本', 409)
    within(provider, asset['payload']['source'], asset['payload']['scope_cid'])
    for owned in asset['payload'].get('owned', []):
        path = strm.target(strm.root(), str(Path(owned['path']).relative_to(strm.root())))
        if not path.is_file() or strm.digest(path.read_bytes()) != owned['digest']:
            raise CloudError('新版本附属输出变化，保留旧版本', 409)
    return provider


def prune(ref):
    """Only recorded bytes, after verified source deletion; foreign sidecars survive."""
    asset, bound = ref['asset'], ref['bound']
    base = strm.root()
    if str(base) != asset['payload']['output_root'] or [base.stat().st_dev, base.stat().st_ino] != asset['payload']['root_identity']:
        raise CloudError('输出根目录身份变化，保留旧引用', 409)
    file = strm.target(base, str(Path(bound['path']).relative_to(base)))
    if file.exists() and (not file.is_file() or strm.digest(file.read_bytes()) != asset['payload']['digest']):
        raise CloudError('旧 STRM 已被修改，保留本地文件', 409)
    with store.db() as con:
        active = con.execute("SELECT payload FROM cloud_assets WHERE id!=? AND state IN ('active','publishing','needs_review')", (asset['id'],)).fetchall()
    shared = {o['path'] for raw, in active for o in json.loads(raw).get('owned', [])}
    file.unlink(missing_ok=True)
    retained = []
    for owned in asset['payload'].get('owned', []):
        path = strm.target(base, str(Path(owned['path']).relative_to(base)))
        if owned['path'] in shared:
            continue
        if path.is_file() and strm.digest(path.read_bytes()) == owned['digest']:
            path.unlink()
        elif path.exists():
            retained.append(str(path))
    governance.local_removed(bound)
    return retained


def complete(job_id, new_asset=None, refs=None):
    """An explicit resume verifies prior writes; governance never replays unknown deletes."""
    with _LOCK, strm._LOCK:
        if new_asset and refs:
            with store.db() as con:
                for ref in refs:
                    payload = {**ref, 'replacement_source': new_asset['payload']['source']}
                    con.execute("INSERT OR IGNORE INTO cloud_upgrades VALUES(?,?,?,?,?,?,?,?)", (store.ident(),job_id,new_asset['id'],ref['asset']['id'],'pending',store.encode(payload),'{}',time.time()))
        records = [r for r in _records(job_id) if r['state'] != 'done' and (not new_asset or r['new_asset'] == new_asset['id'])]
        try:
            # Preflight every old group before any new old-source deletion.
            for record in records:
                provider = check_new(record['new_asset'], record['payload'])
            if records:
                validate_refs([r['payload'] for r in records if not r['result'].get('remote_verified')], provider)
            for record in records:
                ref, result = record['payload'], record['result']
                result.pop('reason', None)
                provider = check_new(record['new_asset'], ref)
                if not result.get('remote_verified'):
                    _save(record, 'deleting', result)
                    governance.delete_sources(ref['bound'], Path(ref['asset']['payload']['output_root']), {r['payload']['bound']['path'] for r in _records(job_id)})
                    result['remote_verified'] = True
                    _save(record, 'remote_deleted', result)
                else:
                    for source in ref['bound']['sources']:
                        previous = governance.record(ref['bound']['account_id'], source['id'])
                        if not previous or not governance._proof(provider, source, previous):
                            raise CloudError('旧源删除回执无法核实，保留旧引用', 409, uncertain=True)
                _save(record, 'pruning', result)
                result['retained_sidecars'] = prune(ref)
                _save(record, 'done', result)
        except (CloudError, OSError, ValueError) as error:
            if not isinstance(error, CloudError):
                error = CloudError('本地输出核验或清理失败，请核对挂载后继续', 409)
            if records:
                current = record if 'record' in locals() else records[0]
                _save(current, 'needs_review', {**current['result'], 'reason': str(error)})
            error.upgrades = summary(job_id)
            raise error
        return summary(job_id)
