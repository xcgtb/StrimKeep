"""Share-library version decisions and owned local handovers. No remote mutations."""
import copy
import time
from pathlib import Path, PurePosixPath
from app.cloud import quality8, store, strm, emby_library
from app.cloud.errors import CloudError


class VersionHeld(CloudError):
    """An intentional quality decision, not an execution failure."""


def token(asset):
    return strm.digest(store.encode(asset).encode())


def reference(asset):
    return {'id': asset['id'], 'path': asset['local_path'], 'token': token(asset)}


def verify(expected, account_id, base):
    asset = store.asset(expected['id'])
    if not asset or token(asset) != expected['token'] or asset['account_id'] != account_id or asset['state'] != 'active':
        raise CloudError('分享库旧版本登记已变化，请重新预览', 409)
    payload = asset['payload']
    if payload.get('source_kind') != 'share' or payload.get('output_root') != str(base) or payload.get('root_identity') != [base.stat().st_dev, base.stat().st_ino] or strm.root('share') != base:
        raise CloudError('分享库归属或挂载身份变化，保留旧版本', 409)
    try:
        path = strm.target(base, str(Path(asset['local_path']).relative_to(base)))
    except ValueError:
        raise CloudError('旧版本不属于当前分享库输出目录', 409) from None
    if not path.is_file() or strm.digest(path.read_bytes()) != payload.get('digest'):
        raise CloudError('旧版本内容已变化，保留原文件并等待核对', 409)
    return asset


def select(account_id, sid, candidates, cfg, base):
    """Select within each TMDB movie/episode and optional DV/non-DV group."""
    chosen, held, comparisons = [], [], []
    enabled, upgrade = cfg['share_quality_enabled'], cfg['share_quality_mode'] == 'upgrade'
    rules, coexist = cfg['share_quality_rules'], cfg['share_quality_coexist_dv']

    def hold(item, reason, retained=False):
        held.append({'file_id': item['source']['id'], 'name': item['source']['name'], 'reason': reason, 'retained': retained})

    for candidate in candidates:
        candidate['upgrades'] = []
        blocked = False
        losers = []
        if enabled:
            for previous in chosen:
                if quality8.identity(candidate['media']) != quality8.identity(previous['media']):
                    continue
                decision = quality8.compare(quality8.facts(candidate['source'], candidate['media']), quality8.facts(previous['source'], previous['media']), rules, coexist)
                comparisons.append({'new': candidate['source']['name'], 'existing': previous['source']['name'], 'scope': '本批分享', **decision})
                if not decision['coexist']:
                    if decision['result'] <= 0:
                        blocked = True
                    else:
                        losers.append(previous)
        if blocked:
            hold(candidate, '本批已有同作品/同集的更优或同质版本，保留已选版本', True)
            continue
        for previous in losers:
            chosen.remove(previous)
            hold(previous, '本批已选择质量更优的新版本', True)
        chosen.append(candidate)

    # Bound matching candidates, never reject a large library just because it
    # contains many unrelated works. Disabled comparison performs no pool scan.
    ids = []
    identities = list(dict.fromkeys(quality8.identity(item['media']) for item in chosen))
    if enabled:
        with store.db() as con:
            for offset in range(0, len(identities), 100):
                chunk = identities[offset:offset+100]
                clause = '('+' AND '.join("json_extract(payload,'$.media."+key+"') IS ?" for key in ('kind','tmdb_id','season','episode'))+')'
                where = ' OR '.join(clause for _ in chunk)
                ids.extend(con.execute("SELECT id FROM cloud_assets WHERE account_id=? AND state='active' AND json_extract(payload,'$.source_kind')='share' AND json_extract(payload,'$.output_root')=? AND ("+where+") LIMIT 2001", (account_id, str(base), *(v for identity in chunk for v in identity))).fetchall())
                if len(ids) > 2000:
                    raise CloudError('本批匹配的分享版本过多，请缩小作品范围后重新预览', 409)
    existing = [asset for row in ids if (asset := store.asset(row[0])) and quality8.identity(asset['payload']['media']) in identities]
    accepted, paths = [], set()
    for item in chosen:
        row, media = item['source'], item['media']
        key = 'share:'+sid+':'+row['id']
        try:
            for old in existing if enabled else []:
                if old['file_id'] == key or quality8.identity(media) != quality8.identity(old['payload']['media']):
                    continue
                decision = quality8.compare(quality8.facts(row, media), quality8.facts(old['payload']['source'], old['payload']['media']), rules, coexist)
                comparisons.append({'new': row['name'], 'existing': old['payload']['source']['name'], 'asset_id': old['id'], 'scope': '当前账号分享库', **decision})
                if decision['coexist']:
                    continue
                if decision['result'] <= 0:
                    raise VersionHeld('分享库已有更优或同质版本，保留旧版本', 409)
                if not upgrade:
                    raise VersionHeld('新版本更优；当前仅比较，切换分享库执行洗版后重新预览', 409)
                expected = reference(old)
                verify(expected, account_id, base)
                item['upgrades'].append(expected)
            relative = media['directory']+'/'+str(PurePosixPath(media['name']).with_suffix('.strm'))
            destination = strm.target(base, relative)
            if destination.exists():
                with store.db() as con:
                    bound_row = con.execute('SELECT id FROM cloud_assets WHERE local_path=?', (str(destination),)).fetchone()
                bound = store.asset(bound_row[0]) if bound_row else None
                replacement = bound and any(v['id'] == bound['id'] for v in item['upgrades'])
                if not bound or (bound['file_id'] != key and not replacement):
                    raise CloudError('同名 STRM 已存在，保留现有版本；共存需使用不同的命名输出', 409)
                verify(reference(bound), account_id, base)
            if relative in paths:
                raise CloudError('本批命名输出相同，请修正模板后重新预览', 409)
            paths.add(relative)
            accepted.append(item)
        except (CloudError, OSError) as error:
            hold(item, str(error), isinstance(error, VersionHeld))
    return accepted, held, comparisons


def rebind(expected, account_id, row, media, source_context, base, expected_output):
    """Same path: retain signed bytes and commit a new share source atomically."""
    with strm._LOCK:
        old = verify(expected, account_id, base)
        from app.cloud import settings
        if any(settings.get()[k] != v for k,v in expected_output.items()):
            raise CloudError('替换前配置变化，请重新预览', 409)
        if quality8.identity(old['payload']['media']) != quality8.identity(media):
            raise CloudError('分享版本身份不一致，拒绝替换', 409)
        payload = copy.deepcopy(old['payload'])
        payload.update(source=row, media=media, scope_cid=str(row['parent_id']), **source_context)
        payload['replacement'] = {'previous_file_id': old['file_id'], 'previous_sha1': old['payload']['source']['sha1'], 'at': time.time()}
        key = 'share:'+source_context['share_id']+':'+row['id']
        with store.db() as con:
            if con.execute('SELECT 1 FROM cloud_assets WHERE account_id=? AND file_id=? AND id!=?', (account_id, key, old['id'])).fetchone():
                raise CloudError('新来源已绑定其他播放引用，请重新预览', 409)
            changed = con.execute("UPDATE cloud_assets SET file_id=?,payload=?,updated=? WHERE id=? AND state='active' AND file_id=? AND payload=?", (key, store.encode(payload), time.time(), old['id'], old['file_id'], store.encode(old['payload']))).rowcount
            if changed != 1:
                raise CloudError('替换期间旧版本变化，已停止', 409)
        asset = store.asset(old['id'])
        emby_library.watch(asset)
        return asset


def prune_old_subtitles(old, successor, base):
    """Refresh metadata from the new source before removing unused owned subtitles."""
    keep = {v['path'] for v in successor['payload'].get('owned', [])}
    retained = []
    for value in old['payload'].get('owned', []):
        if not value.get('file_id') or value['path'] in keep:
            continue
        path = strm.target(base, str(Path(value['path']).relative_to(base)))
        if strm.root('share') != base or old['payload']['root_identity'] != [base.stat().st_dev, base.stat().st_ino]:
            raise CloudError('换版附件清理期间挂载变化，已停止', 409)
        if path.is_file() and strm.digest(path.read_bytes()) == value['digest']:
            path.unlink()
        elif path.exists():
            retained.append(str(path))
    return retained


def retire(expected, account_id, base, successor):
    """New output was verified first. Only exact owned old files may be pruned."""
    with strm._LOCK:
        old = verify(expected, account_id, base)
        protected = {successor['local_path'], *(v['path'] for v in successor['payload'].get('owned', []))}
        removed, kept = [], []
        for value in [{'path': old['local_path'], 'digest': old['payload']['digest']}, *old['payload'].get('owned', [])]:
            try:
                path = strm.target(base, str(Path(value['path']).relative_to(base)))
            except ValueError:
                raise CloudError('旧附件路径超出分享库，已停止清理', 409) from None
            if str(path) in protected:
                continue
            if strm.root('share') != base or old['payload']['root_identity'] != [base.stat().st_dev, base.stat().st_ino]:
                raise CloudError('清理期间分享库挂载变化，已停止', 409)
            if path.exists():
                if not path.is_file() or strm.digest(path.read_bytes()) != value['digest']:
                    kept.append(str(path))
                    continue
                path.unlink()
                removed.append(str(path))
        with store.db() as con:
            con.execute("UPDATE cloud_assets SET state='revoked',updated=? WHERE id=? AND state='active'", (time.time(), old['id']))
        return {'asset_id': old['id'], 'successor': successor['id'], 'removed': removed, 'retained': kept}
