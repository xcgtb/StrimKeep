"""System health checks in the style of Sonarr's Health page.

Each check answers one question a user actually asks when something stops
working: is the database alive, is the Worker running, is 115/Emby/TMDB
configured, is disk filling up, do we have a recent backup, are tasks stuck.
No live external calls: the page stays fast and does not depend on 115/Emby
being reachable. Actions point at the page that can fix the problem.
"""
import asyncio
import re
import shutil
import time

from fastapi import Depends

OK, WARN, ERROR = 'ok', 'warn', 'error'


def _check(key, title, status, message, detail='', action=''):
    return {'key': key, 'title': title, 'status': status,
            'message': message, 'detail': detail, 'action': action}


def _human_size(value):
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return ('%.1f %s' % (value, unit)) if unit != 'B' else ('%d B' % value)
        value /= 1024
    return '%d B' % value


def _database(store):
    try:
        store.db.execute('SELECT 1').fetchone()
        schema = store.meta_json('schema_version', '')
        size = store.path.stat().st_size if store.path.exists() else 0
        return _check('database', '数据库', OK, 'SQLite 可读写',
                      'schema %s · %s' % (schema or '?', _human_size(size)))
    except Exception as exc:
        return _check('database', '数据库', ERROR, '数据库不可读写',
                      type(exc).__name__)


def _worker(store):
    heartbeat = store.meta_json('worker_heartbeat', None)
    if not isinstance(heartbeat, dict):
        if isinstance(heartbeat, (int, float)):
            age = max(0, time.time() - float(heartbeat))
            if age > 30:
                return _check('worker', '后台 Worker', ERROR,
                              '心跳已过期 %.0f 秒' % age, action='logs')
            return _check('worker', '后台 Worker', OK, '心跳正常',
                          '%.0f 秒前' % age)
        return _check('worker', '后台 Worker', WARN,
                      '还没有收到 Worker 心跳', action='logs')
    age = max(0, time.time() - float(heartbeat.get('at') or 0))
    faults = sorted(name for name, value in (heartbeat.get('lane_faults') or {}).items() if value)
    detail = '心跳 %.0f 秒前 · 整理 %s · 调度 %s' % (
        age, '运行' if heartbeat.get('worker_alive') else '停止',
        '运行' if heartbeat.get('scheduler_alive') else '停止')
    if age > 30:
        return _check('worker', '后台 Worker', ERROR,
                      '心跳已过期 %.0f 秒' % age, detail, 'logs')
    if heartbeat.get('closing'):
        return _check('worker', '后台 Worker', ERROR, 'Worker 正在退出', detail, 'logs')
    if faults:
        return _check('worker', '后台 Worker', ERROR,
                      '任务车道异常：%s' % '、'.join(faults), detail, 'logs')
    if not heartbeat.get('worker_alive') or not heartbeat.get('scheduler_alive'):
        return _check('worker', '后台 Worker', ERROR, '整理或调度已停止', detail, 'logs')
    return _check('worker', '后台 Worker', OK, '后台任务运行正常', detail)


def _commands(store):
    try:
        rows = dict(store.db.execute(
            'SELECT state,count(*) FROM worker_commands GROUP BY state').fetchall())
        oldest = store.db.execute(
            "SELECT MIN(created) FROM worker_commands WHERE state IN ('queued','running')").fetchone()[0]
    except Exception as exc:
        return _check('commands', '后台命令队列', WARN, '队列状态读取失败',
                      type(exc).__name__)
    queued, running = int(rows.get('queued', 0)), int(rows.get('running', 0))
    detail = '排队 %d · 执行中 %d' % (queued, running)
    if oldest and time.time() - float(oldest) > 600:
        return _check('commands', '后台命令队列', WARN,
                      '有命令等待超过 10 分钟', detail, 'logs')
    if queued > 50:
        return _check('commands', '后台命令队列', WARN,
                      '命令积压 %d 条' % queued, detail, 'logs')
    return _check('commands', '后台命令队列', OK, '队列正常', detail)


def _disk(label, path, key, action=''):
    try:
        usage = shutil.disk_usage(str(path))
    except OSError as exc:
        return _check(key, label, ERROR, '目录不可访问', type(exc).__name__, action)
    total = usage.total or 1
    free_gb = usage.free / 1024 ** 3
    percent = usage.free / total
    detail = '剩余 %s / %s（%.0f%%）' % (
        _human_size(usage.free), _human_size(usage.total), percent * 100)
    if percent < 0.03 or free_gb < 1:
        return _check(key, label, ERROR, '磁盘空间严重不足', detail, action)
    if percent < 0.10 or free_gb < 5:
        return _check(key, label, WARN, '磁盘空间偏低', detail, action)
    return _check(key, label, OK, '磁盘空间正常', detail)


def _backup(store):
    directory = store.root / 'config/backups'
    backups = sorted(directory.glob('pre-upgrade-*.zip'),
                     key=lambda item: item.stat().st_mtime, reverse=True)
    if not backups:
        return _check('backup', '备份', WARN,
                      '还没有升级前自动备份', '下次版本升级时会自动生成', 'settings')
    newest = backups[0]
    age_days = max(0, (time.time() - newest.stat().st_mtime) / 86400)
    detail = '%s · %s · %.0f 天前' % (
        newest.name, _human_size(newest.stat().st_size), age_days)
    if age_days > 30:
        return _check('backup', '备份', WARN, '最近备份超过 30 天', detail, 'settings')
    return _check('backup', '备份', OK, '已有可恢复备份', detail, 'settings')


def _tmdb(store):
    if store.get('tmdb_key'):
        return _check('tmdb', 'TMDB', OK, '已配置 API Key', action='settings')
    return _check('tmdb', 'TMDB', WARN, '未配置 API Key',
                  '识别与季集信息会不可用', 'settings')


def _emby(app):
    try:
        connection = app.state.organizer.emby.connection()
    except Exception as exc:
        return _check('emby', 'Emby', ERROR, '连接配置读取失败',
                      type(exc).__name__, 'embyproxy')
    if not connection:
        return _check('emby', 'Emby', WARN, '未配置 Emby 连接',
                      '入库通知与媒体信息写入会跳过', 'embyproxy')
    return _check('emby', 'Emby', OK, '已配置 Emby 连接',
                  str(connection[0]), 'embyproxy')


def _cloud115(store):
    cookie = store.get('cookie') or ''
    match = re.search(r'(?:^|;\s*)UID=(\d+)', cookie)
    if cookie and match:
        return _check('cloud115', '115 网盘', OK, '已登录',
                      'UID %s' % match.group(1), 'account')
    return _check('cloud115', '115 网盘', WARN, '未登录或缺少 UID',
                  '整理、分享与播放取链会不可用', 'account')


def _failures(store):
    try:
        needs_review = store.db.execute(
            "SELECT count(*) FROM organize_jobs WHERE state='needs_review'").fetchone()[0]
        failed_commands = store.db.execute(
            "SELECT count(*) FROM worker_commands WHERE state='failed'").fetchone()[0]
        unread_errors = store.db.execute(
            "SELECT count(*) FROM notifications WHERE read_at IS NULL AND level='error'").fetchone()[0]
    except Exception as exc:
        return _check('failures', '近期失败', WARN, '失败统计读取失败',
                      type(exc).__name__, 'tasks')
    detail = '待核查任务 %d · 失败命令 %d · 未读异常 %d' % (
        needs_review, failed_commands, unread_errors)
    if needs_review or failed_commands:
        return _check('failures', '近期失败', WARN, '有需要处理的失败项', detail, 'tasks')
    if unread_errors:
        return _check('failures', '近期失败', WARN, '有未读异常通知', detail, 'logs')
    return _check('failures', '近期失败', OK, '没有待处理失败项', detail)


def collect_health(app, store):
    checks = [
        _database(store),
        _worker(store),
        _commands(store),
        _disk('数据目录', store.root, 'disk_data'),
        _disk('STRM 目录', app.state.strms, 'disk_strm', 'sync'),
        _backup(store),
        _tmdb(store),
        _emby(app),
        _cloud115(store),
        _failures(store),
    ]
    if any(item['status'] == ERROR for item in checks):
        overall = ERROR
    elif any(item['status'] == WARN for item in checks):
        overall = WARN
    else:
        overall = OK
    return {'generated_at': time.time(), 'overall': overall, 'checks': checks}


def install_health(app, store, admin):
    cache = {'at': 0.0, 'data': None}

    @app.get('/api/v1/system/health', dependencies=[Depends(admin)])
    async def system_health():
        if cache['data'] is not None and time.time() - cache['at'] < 10:
            return cache['data']
        data = await asyncio.to_thread(collect_health, app, store)
        cache['at'], cache['data'] = time.time(), data
        return data

