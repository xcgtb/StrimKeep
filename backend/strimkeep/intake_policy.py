"""Shared identities and admission checks for manual and automatic intake."""
import base64
import hashlib
import json
import re
import time

from .cloud115 import CloudError


MAGNET_HASH = re.compile(r'(?i)[?&]xt=urn:btih:([a-z0-9]+)(?:&|$)')
ED2K_HASH = re.compile(r'(?i)^ed2k://\|file\|[^|]*\|\d+\|([0-9a-f]{32})\|')
ACTIVE = ('queued', 'running', 'paused', 'waiting_login', 'needs_review')
ORIGINS = {'web': '网页', 'telegram': 'Telegram', 'channel': '频道监控', 'inbox': '资源收件箱',
           'subscription': '追更订阅'}


def offline_kind(url):
    scheme = url.split(':', 1)[0].lower()
    return scheme if scheme in ('magnet', 'ed2k') else 'direct'


def offline_identity(url):
    if url.lower().startswith('magnet:?') and (match := MAGNET_HASH.search(url)):
        value = match[1]
        if re.fullmatch(r'[a-z2-7]{32}', value, re.I):
            value = base64.b32decode(value.upper()).hex()
        return 'magnet', value.lower()
    if match := ED2K_HASH.search(url):
        return 'ed2k', match[1].lower()
    return 'url', url


def log_reused(store, kind, origin, job_id):
    store.event(kind, f'任务 {job_id}：{ORIGINS.get(origin, origin)}提交相同分享，已沿用现有任务。',
                job_id=job_id)


def share_identity(action, secret, *, account='', target=''):
    value = json.dumps([action, account, target, secret['share_code']], separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()


def matching_job(store, key, *, completed_seconds=0):
    return store.db.execute(
        """SELECT id,state,config FROM organize_jobs
           WHERE json_valid(config) AND json_extract(config,'$.intake_key')=?
             AND (state IN (?,?,?,?,?) OR (state='completed' AND updated>=?))
           ORDER BY (state='completed'), id DESC LIMIT 1""",
        (key, *ACTIVE, time.time() - completed_seconds),
    ).fetchone()


def matching_legacy_job(store, kind, secret, *, account='', target=''):
    rows = store.db.execute(
        """SELECT id,state,config FROM organize_jobs
           WHERE state IN (?,?,?,?,?) AND json_valid(config)
             AND json_extract(config,'$.intake_key') IS NULL
             AND json_extract(config,'$.job_kind')=?
             AND COALESCE(json_extract(config,'$.account_id'),'')=?
             AND COALESCE(json_extract(config,'$.target_cid'),'')=?
           ORDER BY id DESC""",
        (*ACTIVE, 'transfer' if kind == 'transfer' else 'share_organize', account, target),
    )
    for row in rows:
        previous = store.get(kind + '-secret:' + str(row['id']))
        if previous and previous.get('share_code') == secret['share_code']:
            return row
    return None


def verify_credential(store, row, kind, secret):
    previous = store.get(kind + '-secret:' + str(row['id']))
    if previous and previous.get('receive_code') != secret.get('receive_code'):
        raise CloudError(
            '相同分享已有任务 #%d，提取码不同，请核对已有任务。' % row['id'],
            'INTAKE_CONFLICT', 409)


def verify_existing(store, row, *, expected=None, auto=None, generate_only=None):
    """An existing task can be reused only if its explicit policy matches."""
    config = json.loads(row['config'])
    if ((auto is not None and bool(config.get('intake_auto', config.get('organize_after'))) != auto)
            or (generate_only is not None and bool(config.get('generate_only')) != generate_only)):
        raise CloudError(
            '相同分享已有任务 #%d，请核对已有任务的入库方式。' % row['id'],
            'INTAKE_CONFLICT', 409)
    old_expected = config.get('expected_subscription_video')
    if expected is not None and expected != old_expected:
        if (old_expected is None and row['state'] in ('queued', 'waiting_login')
                and not config.get('receipt') and not config.get('share_generation_started')):
            config['expected_subscription_video'] = expected
            store.jobs.set_config(row['id'], config)
        else:
            raise CloudError(
                '相同分享已有任务 #%d，当前订阅的文件约束需要核查。' % row['id'],
                'INTAKE_CONFLICT', 409)
    return row['id']
