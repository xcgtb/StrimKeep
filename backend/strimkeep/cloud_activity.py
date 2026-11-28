"""Turn verified 115 activity into additive directory sync jobs."""
import asyncio
import logging
import sqlite3
import time

from .cloud115 import CloudError, fatal


INTERVAL = 90
MAX_DIRECTORIES = 24
STATE = 'cloud_sync_activity'
# A delete event on its own cannot authorize removing a local output.
CHANGES = {'1', '2', '5', '6', '14', '17', '18', '20', '23', '24'}


def number(value):
    key = str(value)
    return key if key.isascii() and key.isdecimal() and len(key) <= 24 else ''


def selected_roots(profile):
    if not profile.get('watch_changes'):
        return set()
    return set(profile.get('source_cids') or [profile.get('source_cid')]) - {'', None, '0'}


def change_directories(rows):
    directories, unresolved = set(), False
    for row in rows:
        kind = str(row.get('type'))
        if kind not in CHANGES:
            continue
        # Create, copy and rename directory events identify a whole subtree.
        key = 'file_id' if kind in ('17', '18', '20') else 'parent_id'
        directory = number(row.get(key))
        if not directory:
            unresolved = True
        elif directory != '0':
            directories.add(directory)
    return directories, unresolved


class CloudActivity:
    def __init__(self, engine):
        self.store, self.cloud, self.tasks = engine.store, engine.cloud, engine.tasks

    def profiles(self):
        return {p['id']: p for p in self.store.get('sync_profiles', []) if selected_roots(p)}

    async def tick(self):
        profiles = self.profiles()
        if not profiles:
            return
        account = self.tasks.account()
        self.cloud.use_scope(account, 'cloud-activity')
        self.cloud.reset_ledger()
        state = self.store.meta_json(STATE, {}) or {}
        same_account = state.get('account') == account
        rows = await self.cloud.activity_page()
        if any(not isinstance(row, dict) or not number(row.get('id')) for row in rows):
            raise CloudError('115 操作记录缺少事件编号，已保留同步进度。', 'SCHEMA_CHANGED')
        previous = state.get('cursor', '') if same_account else ''
        cursor = number(rows[0]['id']) if rows else previous
        if account != self.tasks.account():
            return
        if not same_account or not state.get('initialized'):
            self.save(account, cursor, 'active')
            return
        if not rows or cursor == previous:
            if state.get('status') != 'active':
                self.save(account, cursor, 'active')
            return
        offset = next((index for index, row in enumerate(rows) if number(row['id']) == previous), None)
        directories, unresolved = change_directories(rows[:offset])
        reconcile = bool(previous and offset is None) or unresolved or len(directories) > MAX_DIRECTORIES
        scopes = {}
        if not reconcile:
            for cid in sorted(directories):
                try:
                    trail = await self.cloud.directory(cid)
                except CloudError as exc:
                    if fatal(exc):
                        raise
                    reconcile = True
                    break
                if not trail or str(trail[-1]) != cid:
                    reconcile = True
                    break
                trail = tuple(map(str, trail))
                for profile_id, profile in profiles.items():
                    if selected_roots(profile).intersection(trail):
                        scopes.setdefault(profile_id, {})[cid] = trail
        with self.store.transaction(immediate=True):
            if account != self.tasks.account() or self.profiles() != profiles:
                return
            for profile_id, profile in profiles.items():
                matches = scopes.get(profile_id, {})
                roots = sorted(selected_roots(profile) if reconcile else {
                    cid for cid, trail in matches.items() if not set(trail[:-1]).intersection(matches)
                })
                if roots:
                    config = self.tasks.sync_profile_config({**profile, 'mode': 'incremental', 'source_cids': roots})
                    config.update(sync_trigger='cloud_activity', cleanup_missing=False, stale_detect=False)
                    self.tasks.enqueue(config, scheduled=True)
            self.save(account, cursor, 'active')
        if reconcile:
            self.store.event('strm_sync', '115 操作记录范围无法完整定位，已对启用的同步目录安排增量核对。')

    def save(self, account, cursor, status):
        self.store.set_meta(STATE, {'account': account, 'cursor': cursor, 'initialized': True,
                                    'status': status, 'checked': time.time()})

    async def loop(self):
        while True:
            try:
                await self.tick()
            except (CloudError, ValueError) as exc:
                state = self.store.meta_json(STATE, {}) or {}
                status = getattr(exc, 'code', 'CONFIG_UNAVAILABLE')
                if state.get('status') != status:
                    self.store.set_meta(STATE, {**state, 'status': status, 'checked': time.time()})
                    self.store.event('strm_sync', '115 变更同步暂缓：' + str(exc)[:130], level='warn')
            except sqlite3.OperationalError:
                logging.getLogger(__name__).warning('115 变更同步数据库暂不可用', exc_info=True)
            await asyncio.sleep(INTERVAL)
