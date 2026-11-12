"""Share receipts in the existing FIFO, with durable non-replayable intents."""
import json
import re
import time
from urllib.parse import urlsplit,parse_qs
from .cloud115 import CloudError, fatal

LINK = re.compile(r'https?://(?:www\.)?(?:115\.com|115cdn\.com|anxia\.com)/s/[A-Za-z0-9]+(?:\?[^\s<>"，。]*)?',re.I)


def shares(text):
    found=[]
    matches=list(LINK.finditer(text))
    for index, match in enumerate(matches):
        url=match.group().rstrip(').,;，。；）')
        parsed=urlsplit(url);code=parsed.path.split('/')[-1]
        password=parse_qs(parsed.query).get('password',[''])[0]
        if not password:
            extra=re.search(r'(?:提取码|访问码|密码)\s*[:：]?\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])',text[match.end():min(match.end()+60, matches[index+1].start() if index+1 < len(matches) else len(text))])
            if extra:password=extra[1]
        if password and not re.fullmatch('[A-Za-z0-9]{4}',password):raise ValueError('提取码须为四位字母或数字。')
        value={'share_code':code,'receive_code':password}
        if value not in found:found.append(value)
    if not found:
        if re.search(r'(?i)(?:magnet:\?|ed2k://)', text):
            raise ValueError('分享 STRM 只支持 115 分享链接（115.com/s/…）；磁力/ed2k 请用离线下载提交，完成后会自动整理生成 STRM。')
        raise ValueError('没有找到可用的 115 分享链接。')
    if len(found)>10:raise ValueError('一次最多提交 10 个分享链接。')
    return found


class Transfer:
    def __init__(self,engine):self.engine,self.store,self.cloud=engine,engine.store,engine.cloud

    async def enqueue(self,text,dest,auto=False,origin='web',update=None):
        if not re.fullmatch(r'[1-9]\d{0,23}',dest):raise ValueError('请选择一个非根目录作为转存目标。')
        links=shares(text);account=self.engine.tasks.account()
        path=await self.cloud.path_info(dest)
        label=' / '.join(p['name'] for p in path if p['id']!='0')
        if self.engine.tasks.account()!=account:raise ValueError('115 账号已变化，请重新提交。')
        organize=None
        if auto:
            # 目标是否该整理交给 run() 里的 scan_scope_allows 判定（含子目录），
            # 这里只取配置，不因为目标不在"待整理根"列表里就让转存失败。
            try:
                organize=self.engine.tasks.config('organize')
            except ValueError:
                organize=None
            else:
                organize['source_cids']=[dest]
        if update:
            old=self.store.get('transfer-update:'+update,[])
            if old:return old
        ids=[]
        def encrypted(value):return self.store.crypto.encrypt(json.dumps(value,ensure_ascii=False).encode()).decode()
        with self.store.db:
            for link in links:
                config={'job_kind':'transfer','account_id':account,'target_cid':dest,'target_label':label,'origin':origin,'organize_after':organize,'target_path_ids':[str(p['id']) for p in path]}
                jid=self.store.jobs.create(json.dumps(config))
                self.store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('transfer-secret:'+str(jid),encrypted(link)))
                ids.append(jid)
            if update:self.store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('transfer-update:'+update,encrypted(ids)))
        return ids

    async def run(self,jid,config):
        from .core import dumps,UncertainState
        dest=config['target_cid'];await self.cloud.directory(dest)
        secret=self.store.get('transfer-secret:'+str(jid))
        if not secret:raise UncertainState('转存任务缺少分享信息。')
        state=config.get('receipt',{})
        class ChannelShareCancelled(Exception):
            pass
        def save():
            config['receipt']=state
            self.engine.update_job(jid,config=dumps(config))
        if not state.get('items'):
            self.engine.update_job(jid,stage='读取分享内容')
            try:
                state['items']=await self.cloud.share_items(secret)
                state['tree']=await self.share_tree(jid,secret,state['items'])
            except CloudError as exc:
                # 只在转存提交前处理明确取消；不把登录/限流或结果未知当成取消。
                if (config.get('origin') == 'channel' and exc.code in ('CLOUD_REJECTED', 'SHARE_NOT_FOUND')
                        and '分享已取消' in str(exc)):
                    self.engine.update_job(jid,state='partial',stage='分享已取消，已跳过',
                                           message='分享已取消，无法转存；已跳过，不会自动重试。')
                    self.store.event('transfer',f'任务 {jid}：分享已取消，已跳过，不再自动重试。')
                    return
                raise
            if not state['items']:raise UncertainState('分享中没有可保存的内容。')
            save()
        self.engine.boundary(jid)
        # Only receiving and checking a batch needs exclusion from source
        # enumeration and output reconciliation. Share reading may overlap.
        if self.engine.scan_receive_gate.locked():
            self.engine.update_job(jid,stage='等待目录扫描或其他转存核对')
        async with self.engine.scan_receive_gate:
            async with self.engine.cleanup_gate:
                account = self.engine.index.account(config)
                self.engine.sync.invalidate(account)
                try:
                    # Legacy receipts keep their own folder. New transfers go directly to
                    # the configured target and remember only the newly received entries.
                    batch=state.get('batch_cid',dest)
                    if 'before_ids' not in state and state.get('phase') not in ('intent','received'):
                        state['before_ids']=[e['id'] for e in await self.cloud.children(batch)];save()
                    async def receive():
                        state['phase']='intent';save()
                        self.engine.boundary(jid)
                        self.engine.update_job(jid,stage='正在保存到 115')
                        try:
                            await self.cloud.share_receive(secret,state['items'],batch)
                            state['accepted']=True;save()
                        except CloudError as exc:
                            if (config.get('origin') == 'channel' and not state.get('resent') and not state.get('accepted')
                                    and exc.code in ('CLOUD_REJECTED', 'SHARE_NOT_FOUND')
                                    and '分享已取消' in str(exc)):
                                # 首次提交明确被拒，可安全结束；重发时须保留旧请求的不确定性。
                                state.pop('phase', None);save()
                                raise ChannelShareCancelled() from None
                            if fatal(exc):
                                # 明确被拒（登录失效 / 限流）：退回提交前状态，冷却后还能重新提交；
                                # 不能被下面的核对吞成「转存内容未核实」。
                                state.pop('phase', None);save()
                                raise
                            if getattr(exc, 'code', '') == 'ALREADY_SAVED':
                                # 115 按「分享 + 目标目录」记转存记录：同目录再存会被拒。
                                # 这里不换目录重转、也不反复提交，直接把话说清楚。
                                state.pop('phase', None);save()
                                self.engine.update_job(jid, stage='115 提示该分享已转存过')
                                self.store.event('transfer',
                                                 f'⚠️ 任务 {jid}：115 拒绝重复转存（{str(exc)[:80]}），未再次提交。')
                                raise UncertainState(
                                    '这条分享已被 115 标记为「已转存过」（目标目录里已有相同内容），'
                                    '所以这次没有转存任何文件。'
                                    '要再存一次：①换一个目标目录；②或在 115 网页「分享 → 我的转存」里'
                                    '清掉这条记录后重试。') from None
                            # Lost response is not authorization to receive a second copy.
                    import asyncio
                    attempts=int(config.get('verify_attempts') or 8)
                    # 重试间隔沿用「整理」配置里的 retry_interval；为 0 时不等待（测试与着急场景）。
                    base=max(0.0,float((config.get('organize_after') or {}).get('retry_interval', 1.2) or 0))
                    expected={(x['name'],x['is_dir'],0 if x['is_dir'] else x['size']) for x in state['items']}
                    expected_names={e['name'] for e in state['items']}

                    async def verify():
                        """115 受理之后目录条目是异步展开的：等它展开完再判，返回 (是否核实, 条目)。"""
                        current=[]
                        for attempt in range(attempts):
                            self.engine.boundary(jid)
                            listing=await self.cloud.children(batch)
                            current=[e for e in listing if e['id'] not in state.get('before_ids',[])]
                            current=[e for e in current if e['name'] in expected_names]
                            actual={(x['name'],x['is_dir'],0 if x['is_dir'] else x['size']) for x in current}
                            if len(current)==len(expected) and actual==expected and await self.saved_entries(jid,current)==state.get('tree'):
                                return True,current
                            if attempt < attempts-1 and base > 0:
                                await asyncio.sleep(min(4, base + attempt*0.8))
                        return False,current

                    if state.get('phase') not in ('intent','received'):
                        await receive()
                    self.engine.update_job(jid,stage='核查转存结果')
                    verified,current=await verify()
                    if not verified:
                        # 意图已落库但 115 从未确认受理、目录里又没有本批产物：允许一次幂等重发。
                        # 115 对「同分享同目录」的重复转存会返回 ALREADY_SAVED，不会产生副本，
                        # 所以这比让任务永久停在「继续只检查」更安全。
                        if not state.get('accepted') and not state.get('resent'):
                            state['resent']=True;save()
                            self.store.event('transfer',f'任务 {jid}：转存结果未在目录中出现，按已保存意图重试一次。')
                            await receive()
                            self.engine.update_job(jid,stage='再次核查转存结果')
                            verified,current=await verify()
                        if not verified:
                            if state.get('accepted'):
                                raise UncertainState('转存已被 115 受理，但目标目录还没展开完整（大文件夹常见）；'
                                                     '已保留转存记录，稍后恢复该任务以继续核对，请不要重复提交这条分享。')
                            raise UncertainState('转存目录内容仍未完整核实；继续只检查，不重复转存。')
                    state.update(phase='received',received_entries=current);save()
                except ChannelShareCancelled:
                    self.engine.update_job(jid,state='partial',stage='分享已取消，已跳过',
                                           message='分享已取消，无法转存；已跳过，不会自动重试。')
                    self.store.event('transfer',f'任务 {jid}：分享已取消，已跳过，不再自动重试。')
                    return
                finally:
                    self.engine.sync.invalidate(account)
        self.engine.boundary(jid)
        if (config.get('organize_after') and not state.get('organize_job')
                and await self.engine.scan_scope_allows([dest])):
            with self.store.db:
                child={**config['organize_after'], 'transfer_parent':jid,
                       'source_cids':[batch], 'cleanup_source_cids':[dest],
                       'transfer_batch_cid':batch, **({'source_entries':current} if batch==dest else {})}
                now=time.time()
                state['organize_job']=self.store.jobs.create(child)
                config['receipt']=state
                self.store.jobs.set_config(jid, config)
        if config.get('origin')=='channel':
            # 频道转存不自动整理，但要把下一轮扫描提前，别让「空扫描退避」把新文件压一小时。
            self.engine.tasks.wake('organize')
        message=f'已转存到 {config.get("target_label","目标目录")}，共 {len(current)} 个文件或文件夹。'
        if state.get('organize_job'):message+=f'整理任务 #{state["organize_job"]} 已入队。'
        if config.get('origin') == 'channel':
            roots = {str(cid) for cid in (self.store.get('organize_config', {}).get('source_cids') or [])}
            scope = set(config.get('target_path_ids') or [dest])
            if not roots.intersection(scope):
                message += '目标目录未被已配置的待整理目录覆盖，请检查扫描范围。'
            elif not (self.store.get('schedule_config', {}) or {}).get('scan_enabled'):
                message += '定时扫描未启用，等待手动整理或开启定时扫描。'
        self.engine.update_job(jid,state='completed',stage='转存完成',manifest=1,message=message)

    async def share_tree(self,jid,secret,items):
        stack=[((),items)];result=[];seen=set()
        while stack:
            self.engine.boundary(jid)
            path,entries=stack.pop()
            for entry in entries:
                if entry['id'] in seen:raise CloudError('分享目录重复出现。','INCOMPLETE_LIST',409)
                seen.add(entry['id'])
                relative=(*path,entry['name'])
                result.append([list(relative),entry['is_dir'],0 if entry['is_dir'] else entry['size']])
                if entry['is_dir']:stack.append((relative,await self.cloud.share_items(secret,entry['id'])))
        return sorted(result,key=lambda x:x[0])

    async def saved_entries(self,jid,entries):
        result=[];stack=[((),entries)];seen=set()
        while stack:
            self.engine.boundary(jid)
            path,items=stack.pop()
            for entry in items:
                if entry['id'] in seen:raise CloudError('转存目录循环，稍后重新读取。','INCOMPLETE_LIST',409)
                seen.add(entry['id']);relative=(*path,entry['name'])
                result.append([list(relative),entry['is_dir'],0 if entry['is_dir'] else entry['size']])
                if entry['is_dir']:stack.append((relative,await self.cloud.children(entry['id'])))
        return sorted(result,key=lambda x:x[0])

    async def saved_tree(self,jid,cid):
        return await self.saved_entries(jid,await self.cloud.children(cid))
