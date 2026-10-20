"""One catalogue for upstream and generated STRM; filesystem scans remain background work."""
from pathlib import Path
from importlib import import_module


def _engine():
    return import_module('.engine', __package__) if __package__ else import_module('engine')


def _settings():
    if not __package__:
        import cloud_bridge
        cloud_bridge.service()
    from app.cloud import settings
    return settings.get()


def definitions():
    engine = _engine()
    cfg = _settings()
    main = engine.RUNTIME_CFG
    return [
        dict(id='upstream_local', origin='upstream', kind='local', label='上游个人库', root=str(engine.L_ROOT), emby_root=engine.EMBY_PATHS.local, governance=True),
        dict(id='upstream_share', origin='upstream', kind='share', label='上游分享库', root=str(engine.S_ROOT), emby_root=engine.EMBY_PATHS.share, governance=True),
        dict(id='generated_local', origin='generated', kind='local', label='自建个人库', root=cfg['output_root'], emby_root=main.get('emby_generated_local_path', ''), governance=cfg['governance_generated_enabled']),
        dict(id='generated_share', origin='generated', kind='share', label='自建分享库', root=cfg['share_output_root'], emby_root=main.get('emby_generated_share_path', ''), governance=cfg['governance_generated_enabled']),
    ]


def extra_mappings(config):
    # Default engine/CLI imports must not create/open a cloud database.
    if not any(config.get(k) for k in ('emby_generated_local_path', 'emby_generated_share_path')):
        return []
    cfg = _settings()
    return [dict(id='generated_'+kind, origin='generated', kind=kind,
                 label='自建个人库' if kind == 'local' else '自建分享库', root=cfg[key], emby_root=config.get(field, ''))
            for kind, key, field in [('local','output_root','emby_generated_local_path'), ('share','share_output_root','emby_generated_share_path')]
            if config.get(field)]


def root_for(path, local, *, mutation=False):
    domain = import_module('.domain.media_sources', __package__) if __package__ else import_module('domain.media_sources')
    entries = [r for r in definitions() if r['kind'] == ('local' if local else 'share')]
    row = domain.select(str(path), entries, 'root')
    if not row or (mutation and not row['governance']):
        raise ValueError('路径不属于已启用的治理来源；自建库需先开启治理联动')
    return Path(row['root'])


def status():
    engine = _engine()
    state = import_module('.state_store', __package__) if __package__ else import_module('state_store')
    previous = state.read(engine.STATE_DIR / 'library_stats_cache.json', {})
    observed = {row['id']: row for row in previous.get('sources', [])}
    rows = []
    definitions_now = definitions()
    for spec in definitions_now:
        old = observed.get(spec['id'], {})
        same = old.get('root') == spec['root']
        conflict = bool(spec['emby_root']) and any(other['id'] != spec['id'] and other['emby_root'].rstrip('/') == spec['emby_root'].rstrip('/') and (other['root'], other['kind']) != (spec['root'], spec['kind']) for other in definitions_now)
        rows.append({**spec, 'count': old.get('count') if same else None,
                     'duplicates': old.get('duplicates', 0) if same else 0,
                     'verified_ts': old.get('verified_ts', 0) if same else 0,
                     'message': ('Emby 路径与另一来源重复，禁止猜测映射' if conflict else
                                 '尚未配置此来源的 Emby 路径' if not spec['emby_root'] else ''),
                     'scan_error': old.get('error', '') if same else '',
                     'mapping_ready': bool(spec['emby_root']) and not conflict})
    stats = import_module('.stats', __package__) if __package__ else import_module('stats')
    return {'status': 'success', 'items': rows, 'ts': previous.get('ts', 0), 'scan': stats.scan_status(),
            'notice': '个人与分享是库类型，上游与自建是来源；同一实际 STRM 文件只计一次。'}


def validate_config(config):
    from app.domain.media_sources import normalize
    cfg = _settings()
    engine = _engine()
    pairs = [('emby_local_path', str(engine.L_ROOT), 'local'),
             ('emby_share_path', str(engine.S_ROOT), 'share'),
             ('emby_generated_local_path', cfg['output_root'], 'local'),
             ('emby_generated_share_path', cfg['share_output_root'], 'share')]
    seen = {}
    for key, root, kind in pairs:
        value = normalize(config.get(key) or (engine._cfg.DEFAULTS[key] if key in ('emby_local_path', 'emby_share_path') else ''))
        if not value:
            continue
        if '\x00' in value or '..' in value.split('/') or not (value.startswith('/') or (len(value) > 2 and value[1:3] == ':/')):
            raise ValueError('Emby 媒体路径必须是无上级跳转的绝对目录')
        if value in seen and seen[value] != (root, kind):
            raise ValueError('同一 Emby 路径不能指向不同媒体来源，请分别配置上游和自建目录')
        seen[value] = (root, kind)
