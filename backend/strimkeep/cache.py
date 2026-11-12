"""Reuse measurements only for an account-bound, unchanged content fingerprint."""
import json
import re
import time

from .media import PROBE_UA, media_from_filename
from .probe import PROBE_REVISION


class MediaCache:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    @staticmethod
    def fingerprint(source):
        sha, size = source.get('sha1'), source.get('size')
        return (sha.upper(), size) if (isinstance(sha, str) and re.fullmatch(r'[a-fA-F0-9]{40}', sha)
                                      and type(size) is int and size > 0) else None

    @staticmethod
    def sufficient(media, policy=None):
        if not isinstance(media, dict) or not all(media.get(k) for k in ('width', 'height', 'codec')):
            return False
        # 旧口径把 8-bit 无传输特性的片子记成「未知」。这类记录要重新探测一次，
        # 否则洗版比较会一直拿陈旧的未知值去比对。
        if media.get('bit_depth') is not None and type(media['bit_depth']) is not int:
            return False
        if media.get('dynamic_range') == '未知' and media.get('probe_revision') != PROBE_REVISION:
            return False
        enabled = {r['key'] for r in (policy or {}).get('rules', []) if r.get('enabled')}
        if (policy or {}).get('dynamic_groups') == 'dovi': enabled.add('dynamic_range')
        fields = {'fps': 'fps', 'bit_depth': 'bit_depth', 'dynamic_range': 'dynamic_range', 'audio': 'audio'}
        return all(media.get(field) for key, field in fields.items() if key in enabled)

    def put(self, account, source, media, raw_name):
        key = self.fingerprint(source)
        if not key or not self.sufficient(media): return
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO media_cache VALUES(?,?,?,?,?,?,?)',
                (account, source['id'], *key, json.dumps(media), raw_name, time.time()))
            # 再按内容指纹存一份：转存/重新分享会换 file_id，但 sha1+size 不变，
            # 下次同内容的新文件可以直接命中，不用重新 ffprobe。
            self.store.db.execute('INSERT OR REPLACE INTO media_by_sha VALUES(?,?,?,?,?,?)',
                (account, key[0], key[1], json.dumps(media), raw_name, time.time()))

    def comparison_facts(self, job_id, source, raw_name):
        """Use validated saved measurements or explicit filename data without probing."""
        config = json.loads(self.engine.job(job_id)['config'])
        account = self.engine.index.account(config)
        key = self.fingerprint(source)
        cached = self.store.db.execute('SELECT * FROM media_cache WHERE account_id=? AND file_id=?',
                                       (account, source['id'])).fetchone()
        if key and cached and (cached['sha1'], cached['size']) == key:
            return json.loads(cached['media']), cached['raw_name'], 'cached'
        row = self.store.db.execute('SELECT i.original,i.plan,j.config FROM organized_files o JOIN organize_items i ON i.id=o.item_id JOIN organize_jobs j ON j.id=o.job_id WHERE o.file_id=?', (source['id'],)).fetchone()
        if key and row:
            original, plan, previous = (json.loads(row[k]) for k in ('original','plan','config'))
            if previous.get('account_id') == account and self.fingerprint(original) == key and isinstance(plan.get('media'), dict):
                return plan['media'], original['name'], 'managed-cache'
        return media_from_filename(source['name']), raw_name, 'filename'

    async def measure(self, job_id, source, raw_name=None, policy=None):
        config = json.loads(self.engine.job(job_id)['config'])
        account = self.engine.index.account(config)
        raw_name = raw_name or source['name']
        key = self.fingerprint(source)
        cached = self.store.db.execute('SELECT * FROM media_cache WHERE account_id=? AND file_id=?',
                                       (account, source['id'])).fetchone()
        if key and cached and (cached['sha1'], cached['size']) == key:
            media = json.loads(cached['media'])
            if self.sufficient(media, policy): return media, cached['raw_name'], 'cached'
        # Import the full measurements from pre-0.5 task records, never infer
        # technical facts by reverse-parsing the shortened canonical filename.
        # 同一份内容换了个 file_id（转存/换来源）：按 sha1+size 直接复用，省掉一次真探。
        # 只在「本账号下这个文件完全没有记录」时用：有记录就得以文件级缓存为准，
        # 否则人为标脏的旧记录会被内容索引绕过。
        if key and not cached:
            seen = self.store.db.execute(
                'SELECT media,raw_name FROM media_by_sha WHERE account_id=? AND sha1=? AND size=?',
                (account, key[0], key[1])).fetchone()
            if seen:
                media = json.loads(seen['media'])
                if self.sufficient(media, policy):
                    name = seen['raw_name'] or raw_name
                    self.put(account, source, media, name)
                    return media, name, 'sha-cache'
        row = self.store.db.execute('SELECT i.original,i.plan,j.config FROM organized_files o JOIN organize_items i ON i.id=o.item_id JOIN organize_jobs j ON j.id=o.job_id WHERE o.file_id=?', (source['id'],)).fetchone()
        if key and row:
            original, plan, previous = (json.loads(row[k]) for k in ('original','plan','config'))
            if (previous.get('account_id') == account and self.fingerprint(original) == key
                    and self.sufficient(plan.get('media'), policy)):
                self.put(account, source, plan['media'], original['name'])
                return plan['media'], original['name'], 'managed-cache'
        self.engine.boundary(job_id)
        ua = PROBE_UA
        url, headers = await self.engine.cloud.url(source['pickcode'], ua)
        media = await self.engine.probe(url, {**headers, 'User-Agent': ua})
        actual = await self.engine.cloud.file(source['id'])
        self.engine.check_source(source, actual, source['name'], source['parent_id'])
        self.put(account, actual, media, raw_name)
        return media, raw_name, 'probed'
