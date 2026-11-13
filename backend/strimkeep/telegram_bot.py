"""Opt-in Bot API long polling; numeric sender authorization and durable offsets."""
import asyncio
import hashlib
import json
import re
import time
from .cloud115 import CloudError
from .offline_api import links_in, directory_config, OFFLINE_MAX_LINKS
from .transfer import LINK, shares as parse_shares
from . import share_service
from .message_templates import transfer_received
from .telegram_security import safe_telegram_error

# 磁力与电驴可以自动识别；http/ftp 直链与分享链接容易混，必须显式 /offline。
OFFLINE_AUTO = re.compile(r'(?i)\b(magnet:\?|ed2k://)')
PLAYABLE = {'.mkv', '.mp4', '.m4v', '.mov', '.avi', '.ts', '.m2ts', '.webm', '.wmv', '.flv', '.rmvb'}

# 机器人指令：key 是命令名（不带斜杠），value 是菜单与开关里显示的名字。
BOT_COMMANDS = {
    'status': '任务状态',
    'tasks': '最近任务',
    'organize': '立即整理影视',
    'sync': '同步 STRM',
    'shareorganize': '整理分享 STRM',
    'share': '分享 STRM（免转存）',
    'offline': '提交离线下载',
    'sign': '115 签到',
}


def bot_commands(store):
    """指令开关：默认全开，只存被关掉的那些。"""
    saved = store.get('bot_commands_config', {}) or {}
    return {name: bool(saved.get(name, True)) for name in BOT_COMMANDS}


def sync_profile_revision(profile):
    """Bind a button to the settings the user actually saw."""
    raw = json.dumps(profile, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:10]


class TelegramBot:
    def __init__(self,engine):
        self.engine,self.store,self.http=engine,engine.store,engine.http
        self.task=None

    def _safe_error(self, exc):
        token = self.store.get('notification_config', {}).get('bot_token')
        return safe_telegram_error(exc, token)

    def start(self):self.task=asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)

    async def sync_menu(self, key, chat):
        profiles = self.store.get('sync_profiles', []) or []
        if not profiles:
            await self.engine.notifications.publish(key, '🔄 STRM 同步',
                '还没有同步任务。请先在网页的 STRM 生成页面创建任务。',
                chat_override=str(chat['id']), force=True)
            return
        token = self.store.get('notification_config', {}).get('bot_token')
        if not token: raise ValueError('Telegram 机器人尚未配置。')
        buttons=[]
        for profile in profiles[:30]:
            ident=str(profile.get('id') or '')
            if not re.fullmatch(r'[a-z0-9]{1,32}', ident): continue
            mode='全量' if profile.get('mode') == 'full' else '增量'
            label=str(profile.get('name') or '同步任务')
            buttons.append([{'text':f'{label} · {mode}',
                             'callback_data':f'sync:{ident}:{sync_profile_revision(profile)}'}])
        if not buttons: raise ValueError('没有可执行的同步任务，请检查网页任务设置。')
        response = await self.engine.notifications.send(token, 'sendMessage', {
            'chat_id':str(chat['id']), 'text':'🔄 选择要立即执行的 STRM 同步任务：\n点击后按网页中保存的目录和方式执行，定时不变。',
            'reply_markup':{'inline_keyboard':buttons}})
        response.raise_for_status()
        if not response.json().get('ok'): raise ValueError('任务菜单发送失败，请稍后重试。')

    async def sync_callback(self, update, config, identity):
        callback = update.get('callback_query') or {}
        sender = callback.get('from') or {}; message = callback.get('message') or {}; chat = message.get('chat') or {}
        if (sender.get('is_bot') or chat.get('type') != 'private'
                or str(sender.get('id')) != config.get('allowed_user_id')
                or str(chat.get('id')) != str(sender.get('id'))): return
        callback_id = callback.get('id')
        if not isinstance(callback_id, str) or not callback_id: return
        token = self.store.get('notification_config', {}).get('bot_token')
        if not token: return
        async def answer(text):
            try:
                await self.http.post('https://api.telegram.org/bot'+token+'/answerCallbackQuery',
                                     json={'callback_query_id':callback_id,'text':text[:180]},timeout=15)
            except Exception:
                pass
        match = re.fullmatch(r'sync:([a-z0-9]{1,32}):([a-f0-9]{10})', str(callback.get('data') or ''))
        if not match: return
        if not bot_commands(self.store).get('sync', True):
            await answer('同步指令已关闭。'); return
        profile = next((p for p in (self.store.get('sync_profiles', []) or []) if p.get('id') == match[1]), None)
        if not profile or sync_profile_revision(profile) != match[2]:
            await answer('任务已删除或修改，请重新发送 /sync。'); return
        marker='bot-sync-click:'+hashlib.sha256((identity+':'+callback_id).encode()).hexdigest()
        with self.store.db:
            cursor=self.store.db.execute('INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)',
                                         (marker,json.dumps({'at':time.time()})))
        if not cursor.rowcount:
            await answer('这次点击已处理。'); return
        try:
            details=self.engine.tasks.sync_profile_config(profile)
            jid,merged=self.engine.tasks.enqueue(details)
        except (ValueError, CloudError) as exc:
            await answer('任务未入队，请查看消息。')
            await self.engine.notifications.publish(f'bot:{identity}:{update["update_id"]}',
                '⚠️ STRM 同步未提交',self._safe_error(exc),chat_override=str(chat['id']),force=True)
            return
        await answer('任务已加入队列。')
        labels=details.get('directory_labels') or {}; roots=details.get('source_cids') or []
        mode='全量同步' if details.get('mode')=='full' else '增量同步'
        result=f'任务：{profile.get("name", "STRM 同步")}\n方式：{mode}\n队列：#{jid}'
        if merged: result+='（已合并到等待中的相同任务）'
        result+='\n目录：\n'+'\n'.join('• '+str(labels.get(cid) or cid) for cid in roots)
        await self.engine.notifications.publish(f'bot:{identity}:{update["update_id"]}',
            '🔄 STRM 同步已提交',result,chat_override=str(chat['id']),force=True,job_id=jid)

    async def handle(self,update,config,identity):
        if isinstance(update.get('callback_query'), dict):
            await self.sync_callback(update,config,identity)
            return
        message=update.get('message',{})
        text=message.get('text') or message.get('caption') or ''
        for entity in message.get('entities',[])+message.get('caption_entities',[]):
            if entity.get('type')=='text_link':text+='\n'+str(entity.get('url',''))
        if not isinstance(text,str):return
        sender=message.get('from',{});chat=message.get('chat',{})
        # Only an explicitly authorized private user can invoke cloud mutations.
        if (sender.get('is_bot') or chat.get('type')!='private' or
                str(sender.get('id'))!=config.get('allowed_user_id') or str(chat.get('id'))!=str(sender.get('id'))):return
        key=f'bot:{identity}:{update["update_id"]}'
        if text.strip().split(' ',1)[0].split('@')[0] in ('/start','/help'):
            await self.engine.notifications.publish(key,'转存机器人',
                '📥 直接发送 115 分享链接即可转存，支持附带提取码。\n整理联动开启时会自动整理并生成 STRM。\n'
                '🔗 /share 分享链接 → 免转存直接生成 STRM（不占网盘空间，新链接自动整理入库）\n'
                '🧲 发送磁力/电驴链接，或 /offline 加链接，即提交 115 离线下载。\n'
                '/organize 立即整理影视（网盘转存的文件）\n'
                '/shareorganize 立即整理待处理的分享 STRM\n'
                '/sync 选择并立即执行 STRM 同步任务\n'
                '/status 查看任务状态，/tasks 看最近任务\n'
                '/sign 115 签到',
                chat_override=str(chat['id']),force=True)
            return
        command=text.strip().split(' ',1)[0].split('@')[0]
        name=command.lstrip('/')
        if name in BOT_COMMANDS and not bot_commands(self.store).get(name, True):
            await self.engine.notifications.publish(key,'⚙️ 指令已关闭',
                '/%s 已在「系统设置 → Telegram 通知」里关闭。' % name,chat_override=str(chat['id']),force=True)
            return
        if command=='/tasks':
            rows=self.store.db.execute('SELECT id,state,config FROM organize_jobs ORDER BY id DESC LIMIT 8').fetchall()
            if not rows:
                result='暂无任务记录。'
            else:
                kinds={'organize':'影视整理','share_organize':'分享整理','strm_sync':'STRM 同步','transfer':'转存'}
                states={'completed':'完成','running':'执行中','queued':'排队','paused':'暂停','partial':'部分完成','needs_review':'待核实','cancelled':'已取消','waiting_login':'待登录'}
                lines=[]
                for r in rows:
                    try: kind=json.loads(r['config']).get('job_kind','organize')
                    except (TypeError, ValueError): kind='organize'
                    lines.append('#%d · %s · %s' % (r['id'], kinds.get(kind,kind), states.get(r['state'],r['state'])))
                result='\n'.join(lines)
            await self.engine.notifications.publish(key,'📋 最近任务',result,chat_override=str(chat['id']),force=True)
            return
        if command=='/sign':
            try:
                r=await self.engine.cloud.sign()
                result=r.get('message') or '完成'
                if r.get('days'): result+='（连续 %s 天）' % r['days']
            except CloudError as exc:
                result=self._safe_error(exc)
            await self.engine.notifications.publish(key,'☑️ 115 签到',result,chat_override=str(chat['id']),force=True)
            return
        if command=='/status':
            counts=dict(self.store.db.execute('SELECT state,count(*) FROM organize_jobs GROUP BY state').fetchall())
            text=f'执行中：{counts.get("running",0)}\n等待执行：{counts.get("queued",0)}\n暂停：{counts.get("paused",0)}'
            await self.engine.notifications.publish(key,'📋 当前任务',text,chat_override=str(chat['id']),force=True)
            return
        if command == '/sync':
            await self.sync_menu(key, chat)
            return
        if command == '/organize':
            try:
                kind='organize' if command=='/organize' else 'strm_sync'
                details=self.engine.tasks.config(kind)
                jid,_=self.engine.tasks.enqueue(details)
                result='已安排'+('影视整理' if kind=='organize' else 'STRM 同步')+'，完成后按通知设置发送结果。'
                if kind=='strm_sync':
                    roots=details.get('source_cids') or [];labels=details.get('directory_labels') or {}
                    if roots:
                        result+=f'\n\n任务：#{jid}\n同步目录：{len(roots)} 个\n'+'\n'.join(f'{labels.get(cid,cid)} · ID {cid}' for cid in roots)
            except ValueError as exc:result=self._safe_error(exc)
            await self.engine.notifications.publish(key,'已接收指令',result,chat_override=str(chat['id']),force=True)
            return
        if command=='/shareorganize':
            pending=share_service.pending_count(self.store)
            if not pending:
                await self.engine.notifications.publish(key,'📤 分享 STRM','没有待整理的分享 STRM。',
                                                         chat_override=str(chat['id']),force=True)
                return
            try:
                jid=share_service.enqueue_organize(self.engine,origin='telegram')
            except Exception as exc:
                await self.engine.notifications.publish(key,'📤 分享 STRM',
                    '整理任务没能入队：'+self._safe_error(exc)[:120],chat_override=str(chat['id']),force=True)
                return
            await self.engine.notifications.publish(key,'📤 分享 STRM',
                f'整理任务 #{jid} 已入队（{pending} 条待整理），完成后按通知设置发送结果。',
                chat_override=str(chat['id']),force=True,job_id=jid)
            return
        if command=='/share':
            mixed=bool(OFFLINE_AUTO.search(text))
            await self._handle_share(text,key+':share' if mixed else key,chat)
            if mixed:await self._handle_offline(text,key+':offline',chat)
            return
        offline=None
        target=''
        if command=='/offline':
            offline=text.strip().partition(' ')[2]
        elif OFFLINE_AUTO.search(text):
            offline=text
        if offline is not None:
            has_share=await self._handle_offline(offline,key+':offline' if LINK.search(text) else key,chat)
            if not has_share:return
            key += ':transfer'
        if not LINK.search(text):return
        dest=config.get('bot_target_cid')
        if dest is None:dest=config.get('target_cid','')
        if text.startswith('/save '):
            parts=text.split(maxsplit=2)
            if len(parts)!=3:return
            dest,text=parts[1:]
        try:
            ids=await self.engine.transfer.enqueue(text,dest,config.get('bot_auto_organize',False),'telegram',identity+':'+str(update['update_id']))
        except ValueError as exc:
            title='⚠️ 转存请求未提交';result=self._safe_error(exc)
        except Exception:
            title='⚠️ 转存请求状态未确认';result='请先检查任务中心，确认没有创建任务后再重试，避免重复转存。'
        else:
            label=dest
            try:
                job=self.engine.job(ids[0]) if ids else None
                details=json.loads(job['config']) if job else {}
                label=details.get('target_label') or dest
            except Exception:
                pass
            # dest 是目录 ID：按当前配置取名字，避免回执显示旧目录。
            label=self.store.directory_label(dest, label if label != dest else '')
            result=transfer_received(ids,label,config.get('bot_auto_organize',False))
            title='📥 转存请求 · 已接收'
        await self.engine.notifications.publish(key,title,result,chat_override=str(chat['id']),force=True)

    async def _handle_offline(self,text,key,chat):
        """独立提交磁力、电驴和直链；返回是否还含有 115 分享。"""
        tokens=text.replace('\n',' ').split()
        target=tokens.pop(0) if tokens and tokens[0].isdigit() else ''
        links=links_in(' '.join(tokens))
        has_share=any(LINK.match(url) for url in links)
        urls=[url for url in links if not LINK.match(url)]
        if not urls:
            if not has_share:
                await self.engine.notifications.publish(key,'🧲 离线下载',
                    '用法：\n/offline 磁力或直链（用网页里配置的目录）\n'
                    '一行一个，最多 %d 条。' % OFFLINE_MAX_LINKS,chat_override=str(chat['id']),force=True)
            return has_share
        ignored=max(0,len(urls)-OFFLINE_MAX_LINKS)
        urls=urls[:OFFLINE_MAX_LINKS]
        cfg=directory_config(self.store.get('offline_config',{}) or {})
        from .offline_api import organize_flag_for
        magnets=[u for u in urls if u.lower().startswith('magnet:')]
        ed2ks=[u for u in urls if u.lower().startswith('ed2k://')]
        others=[u for u in urls if not u.lower().startswith(('magnet:','ed2k://'))]
        results=[]
        for group,kind,title in ((magnets,'bot_magnet','磁力'),(ed2ks,'bot_ed2k','ED2K'),(others,'target','直链')):
            if not group:continue
            target_cid=target or cfg.get(kind+'_cid') or ''
            label=target or cfg.get(kind+'_label') or target_cid
            if not target_cid:
                results.append(f'⚠️ {title}未提交：尚未选择目录。');continue
            try:
                sid=await self.engine.offline.submit(group,target_cid,label,origin='bot',
                    organize=organize_flag_for(cfg,'magnet' if kind=='bot_magnet' else ('ed2k' if kind=='bot_ed2k' else 'direct'),'bot'))
                results.append(f'✅ {title}：已提交 {len(group)} 个 · 批次 #{sid}\n📂 115 网盘 / {label}\n⏳ 等待下载完成并核查')
            except CloudError as exc:
                results.append(f'⚠️ {title}：提交结果待核查 · {self._safe_error(exc)}\n请先检查 115 离线任务，避免重复提交。')
            except Exception:
                results.append(f'⚠️ {title}：提交结果未确认。请先检查 115 离线任务，避免重复提交。')
        if ignored:results.append(f'ℹ️ 单次最多 {OFFLINE_MAX_LINKS} 个链接，另有 {ignored} 个未提交。')
        await self.engine.notifications.publish(key,'🧲 离线下载 · 提交结果','\n\n'.join(results),chat_override=str(chat['id']),force=True)
        return has_share

    async def _handle_share(self,text,key,chat):
        """免转存分享 STRM（U03/U04）：整条管线进持久化 FIFO 任务，TG 立即回执。

        解析、生成、TMDB 识别、洗版、Emby 通知全部在 share_service 的
        share_organize 任务里执行；TG 不再阻塞等待整批完成，处理期间
        仍能响应其他命令。
        """
        engine=self.engine
        try:secrets_list=parse_shares(text)
        except ValueError as exc:
            await engine.notifications.publish(key,'🔗 分享 STRM',self._safe_error(exc),chat_override=str(chat['id']),force=True)
            return
        from .share_service import enqueue_organize
        ids=[];failure=False
        for secret in secrets_list:
            try:ids.append(enqueue_organize(engine,origin='telegram',secret=secret))
            except Exception:
                failure=True;break
        if not ids:
            await engine.notifications.publish(key,'⚠️ 分享 STRM · 状态待核查',
                '任务未能确认入队。请先检查任务中心，再决定是否重试。',chat_override=str(chat['id']),force=True)
            return
        numbers='、'.join('#'+str(jid) for jid in ids)
        result=(f'🔗 模式  分享 STRM · 免转存\n📨 已入队  {len(ids)} 个分享\n📋 任务  {numbers}'
                '\n📂 位置  分享 STRM 待整理')
        if failure:result+=f'\n⚠️ 其余 {len(secrets_list)-len(ids)} 个分享未确认，请先检查任务中心再重试。'
        await engine.notifications.publish(key,'🔗 分享 STRM · 已接收',result,chat_override=str(chat['id']),force=True)

    async def _safe_handle(self,update,config,identity):
        """单条消息失败不得卡死轮询：记录原因、回告用户、继续推进 offset。"""
        try:
            await self.handle(update,config,identity)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail=self._safe_error(exc)
            self.store.event('bot','TG 消息处理失败：%s'%detail[:160])
            chat=(update.get('message') or {}).get('chat') or {}
            cb=update.get('callback_query') or {}
            cbchat=(cb.get('message') or {}).get('chat') or {}
            sender=cb.get('from') or {}
            if (cbchat.get('type')=='private' and not sender.get('is_bot')
                    and str(sender.get('id'))==config.get('allowed_user_id')
                    and str(cbchat.get('id'))==str(sender.get('id'))):
                chat=cbchat
            if chat.get('id'):
                try:
                    await self.engine.notifications.publish(
                        'bot:%s:%s:error'%(identity,update.get('update_id')),
                        '⚠️ 这条消息没能处理完',
                        '%s\n已跳过这条，请重试或换一种方式发送。'%detail[:200],
                        chat_override=str(chat['id']),force=True)
                except Exception:
                    pass
            return False

    async def poll(self):
        config=self.store.get('transfer_config',{})
        token=self.store.get('notification_config',{}).get('bot_token')
        # 只开频道监控、不开链接转存时，同一个 bot 仍要轮询才收得到 channel_post。
        watching=bool((self.store.get('channel_monitor') or {}).get('enabled'))
        if not token or (not config.get('bot_enabled') and not watching):return False
        identity=hashlib.sha256((token+str(self.store.get('bot_epoch',''))).encode()).hexdigest()[:24]
        # 菜单版本：加命令后需要重新 setMyCommands，用版本号让缓存失效
        if self.store.get('bot-menu:'+identity+':v4')!='ready':
            response=await self.http.post('https://api.telegram.org/bot'+token+'/setMyCommands',json={'commands':[{'command':'help','description':'使用帮助'},{'command':'status','description':'任务状态'},{'command':'tasks','description':'最近任务'},{'command':'organize','description':'立即整理影视'},{'command':'sync','description':'同步 STRM'},{'command':'shareorganize','description':'整理分享 STRM'},{'command':'share','description':'分享 STRM（免转存）'},{'command':'offline','description':'提交离线下载'},{'command':'sign','description':'115 签到'}]},timeout=15)
            if response.status_code==200 and response.json().get('ok'):self.store.set_many({'bot-menu:'+identity+':v4':'ready'})
        key='bot-offset:'+identity;offset=self.store.get(key,None)
        # Ignore backlog on first activation. Never delete an existing webhook.
        response=await self.http.post('https://api.telegram.org/bot'+token+'/getUpdates',json={
            'offset':-1 if offset is None else offset,'limit':100,'timeout':0 if offset is None else 20,
            'allowed_updates':['message','channel_post','callback_query']},timeout=25)
        response.raise_for_status();data=response.json()
        if self.store.get('transfer_config',{})!=config or self.store.get('notification_config',{}).get('bot_token')!=token:return False
        if not data.get('ok') or not isinstance(data.get('result'),list):raise ValueError('bot response')
        updates=data['result']
        for update in updates:
            if not isinstance(update.get('update_id'),int):raise ValueError('update identity')
            if offset is not None and update['update_id']<offset:continue
            if isinstance(update.get('channel_post'),dict):
                # 频道帖只进监控管线：不做转存，也不接受任何指令。
                try:
                    await self.engine.channels.handle(update)
                except asyncio.CancelledError:raise
                except Exception as exc:
                    self.store.event('channel','频道帖子处理失败：%s'%self._safe_error(exc)[:160])
                self.store.set_many({key:update['update_id']+1})
                continue
            if offset is not None:
                try:
                    await asyncio.wait_for(self._safe_handle(update,config,identity), timeout=900)
                except asyncio.TimeoutError:
                    self.store.event('bot','TG 消息处理超过 15 分钟，已放弃该消息并继续轮询。')
            self.store.set_many({key:update['update_id']+1})
        if offset is None and not updates:self.store.set_many({key:0})
        self.store.set_many({'bot_runtime':{'state':'ready','checked':time.time()}})
        return True

    async def loop(self):
        while True:
            try:
                enabled=await self.poll()
                await asyncio.sleep(1 if enabled else 3)
            except asyncio.CancelledError:raise
            except Exception as exc:
                detail=self._safe_error(exc)
                self.store.event('bot','TG 轮询失败：%s'%detail[:200])
                self.store.set_many({'bot_runtime':{'state':'error','checked':time.time(),'message':'TG 接收暂不可用：%s'%detail[:160]}})
                await asyncio.sleep(10)
