"""Bounded incremental STRM publication; remote mutations stay on the job thread.

Every queued file has a durable step. Restarted jobs require review as before;
queued/dispatched writes are never silently replayed.
"""
import contextvars
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from app.cloud import store, settings, events, notifications
from app.cloud.errors import CloudError

OUTPUT_KEYS = ('public_url', 'output_root', 'emby_output_root', 'nfo_enabled', 'artwork_enabled')


class IncrementalPublisher:
    LIMIT = 4

    def __init__(self, job_id, plan, planned, result):
        from app.cloud import strm
        self.job_id, self.plan, self.result = job_id, plan, result
        self.read_provider = None
        self.snapshot = {k: settings.get()[k] for k in OUTPUT_KEYS}
        root = strm.root()
        self.root_identity = (root.stat().st_dev, root.stat().st_ino)
        self.posters = notifications.BatchPosters(job_id, planned)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cloud-strm-publish')
        self.pending, self.submitted = [], set()
        self.stats = result.setdefault('publication', {})
        events.emit('strm', '增量 STRM 发布队列已启动，最多缓存 '+str(self.LIMIT)+' 个文件', job_id, details={'event':'publish_queue','status':'started','count':self.LIMIT})

    def submit(self, index, source, media, companions, upgrades=None):
        # Backpressure also lets the job thread finish any source replacement.
        while len(self.pending) >= self.LIMIT:
            self.collect(wait=True)
        key = 'publication:'+source['id']
        payload = {'source':source, 'media':media, 'companions':companions, 'output':self.snapshot}
        with store.db() as con:
            changed = con.execute('INSERT OR IGNORE INTO cloud_steps VALUES(?,?,?,?,?,?)', (self.job_id,key,'queued',store.encode(payload),'{}',time.time())).rowcount
        if not changed:
            raise CloudError('此文件已有发布记录，请核对原任务，禁止重复提交', 409, uncertain=True)
        self.submitted.add(index)
        context = contextvars.copy_context()
        future = self.pool.submit(context.run, self._publish, index, key, copy.deepcopy(payload))
        self.pending.append((future, upgrades or []))
        events.emit('strm', '已加入增量发布队列：'+media['directory']+'/'+media['name'], self.job_id, details={'event':'publish_queue','status':'queued','file_id':source['id'],'path':media['directory'],'count':len(self.pending)})
        self.collect()

    def skip(self, index):
        if index in self.submitted:
            return
        self.submitted.add(index)
        context = contextvars.copy_context()
        self.pending.append((self.pool.submit(context.run, self.posters.finish, index), []))
        while len(self.pending) >= self.LIMIT:
            self.collect(wait=True)

    def _publish(self, index, key, payload):
        from app.cloud import workflow, strm
        source, media = payload['source'], payload['media']
        started = time.monotonic()
        with store.db() as con:
            changed = con.execute("UPDATE cloud_steps SET state='dispatched',updated=? WHERE job_id=? AND step_key=? AND state='queued'", (time.time(),self.job_id,key)).rowcount
        if not changed:
            raise CloudError('发布步骤状态变化，需要核对',409)
        try:
            account = store.account(self.plan['account_id'])
            current = settings.get()
            root = strm.root()
            if not account or account['revision'] != self.plan['account_revision'] or any(current[k] != self.snapshot[k] for k in OUTPUT_KEYS) or (root.stat().st_dev,root.stat().st_ino) != self.root_identity:
                raise CloudError('队列等待期间账号、输出配置或挂载变化，请重新预览同步',409)
            if self.read_provider is None:
                # Do not share the organizer's SDK client/session across threads.
                self.read_provider = workflow.for_account(self.plan['account_id'])
            for row in [source, *payload['companions']]:
                if not workflow._snapshot_equal(row,self.read_provider.info(row['id'])):
                    raise CloudError('队列等待期间源文件或附属文件变化，保留现有引用',409)
            asset = strm.generate(self.plan['account_id'],source,media,self.plan['target_cid'],payload['companions'],self.read_provider,expected_output=self.snapshot)
            receipt = {'asset_id':asset['id'],'path':asset['local_path']}
            with store.db() as con:
                con.execute("UPDATE cloud_steps SET state='verified',result=?,updated=? WHERE job_id=? AND step_key=?",(store.encode(receipt),time.time(),self.job_id,key))
            self.stats.setdefault(media['directory'], {'published':0,'failed':0})['published'] += 1
            self.posters.verified(index,source,media)
            events.emit('strm','增量发布已核实：'+asset['local_path'],self.job_id,details={'event':'publication','status':'verified','file_id':source['id'],'asset_id':asset['id'],'path':asset['local_path'],'elapsed_ms':int((time.monotonic()-started)*1000)})
            return asset
        except Exception as error:
            reason = str(error) if isinstance(error, CloudError) else 'STRM 发布异常，请核对文件与任务步骤；未自动重试'
            with store.db() as con:
                state = 'unknown' if not isinstance(error, CloudError) or error.uncertain else 'failed'
                con.execute('UPDATE cloud_steps SET state=?,result=?,updated=? WHERE job_id=? AND step_key=?',(state,store.encode({'reason':reason}),time.time(),self.job_id,key))
            self.stats.setdefault(media['directory'], {'published':0,'failed':0})['failed'] += 1
            events.emit('strm','增量发布未完成：'+source['name']+' · '+reason,self.job_id,level='WARNING',details={'event':'publication','status':'failed','file_id':source['id'],'path':media['directory'],'elapsed_ms':int((time.monotonic()-started)*1000),'reason':reason})
            return {'error':{'file_id':source['id'],'reason':reason}}
        finally:
            self.posters.finish(index)

    def collect(self, wait=False):
        while self.pending and (wait or self.pending[0][0].done()):
            future, upgrades = self.pending.pop(0)
            asset = future.result()
            if asset:
                if 'error' in asset:
                    self.result['errors'].append(asset['error'])
                else:
                    self.result['generated'].append({'asset_id':asset['id'],'path':asset['local_path']})
                    self.result['errors'].extend({'file_id':asset['file_id'],'reason':w} for w in asset['payload'].get('warnings',[]))
                    if upgrades:
                        # Source deletion stays serial with the organizer, never
                        # inside the background publisher or notification code.
                        from app.cloud import replacement
                        try:
                            self.result['upgrades'] = replacement.complete(self.job_id,asset,upgrades)
                        except CloudError as error:
                            self.result['errors'].append({'file_id':asset['file_id'],'reason':str(error)})
                            self.result['upgrades'] = getattr(error,'upgrades',[])
                            if error.uncertain:
                                error.partial_result = self.result
                                raise
            wait = False

    def close(self):
        events.emit('strm','整理阶段结束，等待增量发布队列收尾',self.job_id,details={'event':'publish_queue','status':'draining','count':len(self.pending)})
        # Always join, including stop/error paths, before cleanup or summary.
        failure = None
        try:
            while self.pending:
                try:
                    self.collect(wait=True)
                except Exception as error:
                    failure = failure or error
        finally:
            self.pool.shutdown(wait=True)
            self.posters.flush()
        if failure:
            if isinstance(failure, CloudError):
                failure.partial_result = self.result
            raise failure
        events.emit('strm','增量发布队列已收尾：STRM '+str(len(self.result['generated']))+' 个，异常 '+str(len(self.result['errors']))+' 项',self.job_id,details={'event':'publish_queue','status':'finished','published_count':len(self.result['generated']),'failed_count':len(self.result['errors'])})
