"""Conservative, read-only quality selection for *share* STRM references.

Opted-in upgrade mode chooses the best candidate before publication. Existing
share references remain playable until a separately confirmed NAS-only cleanup;
this module never deletes 115 files or removes the previous STRM.
"""

import hashlib
from pathlib import Path, PurePosixPath

from app.cloud import quality8, store, strm
from app.cloud.errors import CloudError


def _key(row, config):
    values = quality8.facts(row['source'], row['media'])
    bucket = quality8.bucket(values) if config['quality8_coexist_dv'] else 'all'
    return quality8.identity(row['media']), bucket


def _live_asset(asset, base):
    """An unverified persisted asset is not a basis for automatic comparison."""
    if not asset or asset['state'] != 'active' or asset['payload'].get('source_kind') != 'share':
        raise CloudError('已有分享版本未就绪，保留旧引用并暂停自动洗版', 409)
    payload = asset['payload']
    path = Path(asset['local_path'])
    try:
        if (payload.get('output_root') != str(base) or
            payload.get('root_identity') != [base.stat().st_dev, base.stat().st_ino] or
            strm.target(base, str(path.relative_to(base))) != path or
            not path.is_file() or path.is_symlink() or
            strm.digest(path.read_bytes()) != payload['digest']):
            raise CloudError('已有分享 STRM 来源或挂载发生变化，暂停自动洗版', 409)
    except (ValueError, OSError, KeyError):
        raise CloudError('已有分享 STRM 无法核实，暂停自动洗版', 409) from None
    return asset


def select(items, account_id, config, base, share_id):
    """Returns (publishable items, held reasons) without any remote mutation."""
    if not (config['quality8_enabled'] and config['quality8_mode'] == 'upgrade'):
        return items, []
    with store.db() as con:
        ids = [row[0] for row in con.execute(
            "SELECT id FROM cloud_assets WHERE account_id=? AND "
            "json_extract(payload,'$.source_kind')='share' AND "
            "json_extract(payload,'$.output_root')=? AND "
            "state IN ('active','publishing','needs_review')",
            (account_id, str(base)),
        )]
    existing = {}
    for asset_id in ids:
        asset = store.asset(asset_id)
        if asset:
            existing.setdefault(_key({'source': asset['payload']['source'], 'media': asset['payload']['media']}, config), []).append(asset)
    batches = {}
    for item in items:
        batches.setdefault(_key(item,config), []).append(item)
    chosen, held = [], []
    for identity, group in batches.items():
        winner = group[0]
        for candidate in group[1:]:
            result = quality8.compare(quality8.facts(candidate['source'],candidate['media']),
                                      quality8.facts(winner['source'],winner['media']),config['quality8_rules'])
            if result['result'] == 1:
                winner = candidate
        for candidate in group:
            if candidate is not winner:
                held.append({'file_id':candidate['source']['id'],'name':candidate['source']['name'],
                             'reason':'同作品本批已有更优或平局版本；本次不生成，原分享文件保留', 'blocked':False})
        try:
            previous = existing.get(identity, [])
            for old in previous:
                _live_asset(old, base)
            # Immutable comparison evidence: run-time execution must observe the
            # same set of registered versions on the same NAS mount.
            winner['quality_guard'] = [
                {'id':old['id'], 'file_id':old['file_id'], 'path':old['local_path'],
                 'digest':old['payload']['digest']} for old in previous
            ]
            different = [asset for asset in previous if asset['file_id'] != 'share:'+share_id+':'+winner['source']['id']]
            if different:
                new_facts = quality8.facts(winner['source'],winner['media'])
                if any(quality8.compare(new_facts,quality8.facts(old['payload']['source'],old['payload']['media']),config['quality8_rules'])['result'] <= 0 for old in different):
                    held.append({'file_id':winner['source']['id'],'name':winner['source']['name'],
                                 'reason':'新版本未优于已有分享 STRM；平局保留已有版本，不重复生成', 'blocked':False})
                    continue
                # The old playback remains valid. The new reference is published to
                # an independent safe path, never overwriting somebody else's asset.
                suffix=hashlib.sha256((share_id+':'+winner['source']['id']).encode()).hexdigest()[:12]
                winner['media']['directory'] = str(PurePosixPath(winner['media']['directory']) / ('版本-'+suffix))
                winner['quality_decision'] = '新版本更优；仅新增 STRM，旧引用需在入库核验后人工确认清理'
            chosen.append(winner)
        except CloudError as error:
            held.append({'file_id':winner['source']['id'],'name':winner['source']['name'],
                         'reason':str(error), 'blocked':True})
    return chosen, held


def recheck(item, account_id, config, base):
    """Fail closed if a better/shared candidate appeared since quality preview."""
    if not (config['quality8_enabled'] and config['quality8_mode'] == 'upgrade'):
        return
    original = item.get('quality_guard')
    if not isinstance(original, list):
        raise CloudError('分享洗版缺少已核对的旧版本快照', 409)
    identity = _key(item, config)
    with store.db() as con:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM cloud_assets WHERE account_id=? AND json_extract(payload,'$.source_kind')='share' AND "
            "json_extract(payload,'$.output_root')=? AND state IN ('active','publishing','needs_review')",
            (account_id, str(base)),
        )]
    current = []
    for asset_id in ids:
        old = store.asset(asset_id)
        if old and _key({'source':old['payload']['source'], 'media':old['payload']['media']},config) == identity:
            _live_asset(old,base)
            current.append({'id':old['id'], 'file_id':old['file_id'],
                            'path':old['local_path'], 'digest':old['payload']['digest']})
    if sorted(current, key=lambda r:r['id']) != sorted(original,key=lambda r:r['id']):
        raise CloudError('分享同作品已有版本在预览后变化；未覆盖旧文件，请重新洗版预览', 409)
