"""Media overview, live logs and presentation settings."""
import asyncio,calendar,datetime,json,time,shutil
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from fastapi import Depends,HTTPException,Request,Query
from fastapi.responses import StreamingResponse,Response,RedirectResponse
from zoneinfo import ZoneInfo
from pydantic import BaseModel,Field,ConfigDict
import httpx
from .network import emby_request
from .message_templates import cloud_sign_notice, cloud_recycle_notice
from .emby_connection import probe,fingerprint,error_message

class ConsoleConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    theme:str=Field(default='system',pattern='^(light|dark|system)$')
    background_url:str=Field(default='',max_length=2000)
    background_opacity:float=Field(default=.12,ge=0,le=.5)
    strm_container_path:str=Field(default='/strm',max_length=500)
    emby_strm_path:str=Field(default='/strimkeep',max_length=500)

class BotCommands(BaseModel):
    """机器人指令开关：只传指令名到布尔的映射。"""
    model_config=ConfigDict(extra='forbid')
    enabled:dict[str,bool]=Field(default_factory=dict)
    order:list[str]|None=None

class CloudMaintenance(BaseModel):
    """115 签到与回收站清空的定时设置。"""
    model_config=ConfigDict(extra='forbid')
    sign_enabled:bool=False
    sign_cron:str=Field(default='0 9 * * *',max_length=64)
    recycle_enabled:bool=False
    recycle_cron:str=Field(default='0 4 * * 0',max_length=64)
    recycle_password:str=Field(default='000000',pattern='^[0-9]{6}$')

class NetworkConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled:bool=False
    proxy_url:str=Field(default='',max_length=2000)
    proxy_emby:bool=False


def activity_time(value):
    """Emby 活动日志的时间带 7 位小数秒，fromisoformat 解析不了，按 UTC 截到秒。"""
    try:
        return calendar.timegm(time.strptime(str(value or '')[:19], '%Y-%m-%dT%H:%M:%S'))
    except ValueError:
        return 0


def install_console(app,store,admin):
    @app.get('/api/v1/console/config',dependencies=[Depends(admin)])
    async def config():return {'config':{**ConsoleConfig().model_dump(),'strm_container_path':str(app.state.strms),**store.get('console_config',{})}}
    @app.put('/api/v1/console/config',dependencies=[Depends(admin)])
    async def save(body:ConsoleConfig):
        for path in (body.strm_container_path,body.emby_strm_path):
            p=PurePosixPath(path)
            if not p.is_absolute() or '..' in p.parts:raise HTTPException(422,'路径映射须为容器内绝对路径。')
        if body.strm_container_path.rstrip('/')!=str(app.state.strms).rstrip('/'):
            raise HTTPException(422,'程序内路径应与实际 STRM 挂载一致：'+str(app.state.strms))
        if body.background_url and urlsplit(body.background_url).scheme not in ('http','https'):raise HTTPException(422,'背景图片请填写 HTTP/HTTPS 地址。')
        store.set_many({'console_config':body.model_dump()});return await config()
    @app.get('/api/v1/network/config',dependencies=[Depends(admin)])
    async def network():
        c=store.get('network_config',{})
        return {'enabled':c.get('enabled',False),'proxy_emby':c.get('proxy_emby',False),'proxy_configured':bool(c.get('proxy_url'))}
    @app.put('/api/v1/network/config',dependencies=[Depends(admin)])
    async def save_network(body:NetworkConfig):
        c=store.get('network_config',{})
        if body.proxy_url:
            try:
                p=urlsplit(body.proxy_url)
                valid=p.scheme in ('http','https','socks5') and p.hostname and not p.fragment and (p.port is None or 1<=p.port<=65535)
            except ValueError:valid=False
            if not valid:raise HTTPException(422,'请输入有效的 HTTP/HTTPS/SOCKS5 代理地址。')
            c['proxy_url']=body.proxy_url
        if body.enabled and not c.get('proxy_url'):raise HTTPException(422,'请填写代理地址。')
        c.update(enabled=body.enabled,proxy_emby=body.proxy_emby);store.set_many({'network_config':c});return await network()
    @app.get('/api/v1/logs',dependencies=[Depends(admin)])
    async def logs(before:int=Query(2**62),kind:str='',q:str='',limit:int=Query(100,ge=1,le=500)):
        # 0.8.21：events 是最大的表，带 LIKE 的查询放工作线程（一线程一连接由 Store.db 保证）。
        def read():
            rows=store.db.execute('SELECT * FROM events WHERE id<? AND (?=\'\' OR kind=?) AND message LIKE ? ORDER BY id DESC LIMIT ?',(before,kind,kind,'%'+q+'%',limit)).fetchall()
            return {'items':[dict(r) for r in rows]}
        return await asyncio.to_thread(read)
    @app.get('/api/v1/logs/export',dependencies=[Depends(admin)])
    async def export_logs(kind:str='',q:str='',limit:int=Query(5000,ge=1,le=20000)):
        """按当前筛选导出日志（最新在后），返回纯文本附件，方便留档/贴给客服。"""
        def read():
            rows=store.db.execute('SELECT * FROM events WHERE (?=\'\' OR kind=?) AND message LIKE ? ORDER BY id DESC LIMIT ?',(kind,kind,'%'+q+'%',limit)).fetchall()
            lines=[f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['at']))} [{r['kind']}] {r['message']}" for r in reversed(rows)]
            return '\n'.join(lines)+'\n'
        body=await asyncio.to_thread(read)
        name='strimkeep-logs-'+time.strftime('%Y%m%d-%H%M%S')+'.txt'
        return Response(content=body,media_type='text/plain; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{name}"'})
    @app.get('/api/v1/logs/events',dependencies=[Depends(admin)])
    async def live(request:Request,after:int=0):
        async def events():
            cursor=after
            while not await request.is_disconnected():
                if not store.session(request.cookies.get('strimkeep_session','')):break
                rows=await asyncio.to_thread(lambda: store.db.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT 200',(cursor,)).fetchall())
                if rows:
                    cursor=rows[-1]['id'];yield 'event: logs\ndata: '+json.dumps({'items':[dict(r) for r in rows]},ensure_ascii=False)+'\n\n'
                else:yield ': heartbeat\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(events(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no','Cache-Control':'no-cache'})
    @app.get('/api/v1/dashboard',dependencies=[Depends(admin)])
    async def dashboard():
        engine=app.state.organizer
        # P01：只对本地登记聚合做短期缓存（绑定登记表数据版本）；Emby 状态
        # 必须实时反映连接变化，不进入缓存。
        # 0.8.21：这段要遍历全部 organized_files 并逐行解析 metadata（现场 822 行），
        # 放到工作线程；一线程一连接保证 store.db 在那边也是自己的连接。
        def local_view():
            sig=store.db.execute('SELECT count(*),COALESCE(max(rowid),0) FROM organized_files').fetchone()
            cached=getattr(app.state,'dashboard_local_cache',None)
            if cached and cached['key']==sig:
                return cached['local'],cached['recent']
            local={'movies':0,'series':0,'episodes':0}
            works=set()
            recent=[]
            for row in store.db.execute('SELECT o.file_id,o.metadata,o.relative_path FROM organized_files o ORDER BY o.rowid DESC'):
                m=json.loads(row['metadata']);hint=m.get('hint',{});d=m.get('details',{});key=(hint.get('media_type'),d.get('id'))
                if hint.get('media_type')=='tv':local['episodes']+=1
                if key not in works:
                    works.add(key);local['series' if key[0]=='tv' else 'movies']+=1
                    if len(recent)<12:recent.append({'name':d.get('title') or d.get('name') or row['relative_path'],'year':str(d.get('release_date') or d.get('first_air_date') or '')[:4],'poster':d.get('poster_path'),'kind':hint.get('media_type')})
            app.state.dashboard_local_cache={'key':sig,'local':local,'recent':recent}
            return local,recent
        local,recent=await asyncio.to_thread(local_view)
        engine=app.state.organizer;connection=engine.emby.connection();emby={'connected':False,'configured':bool(connection),'counts':{},'playing':[],'recent':[],'history':[],'trend':[]}
        errors=[]
        if connection:
            try:
                result=await probe(app.state.http,store)
                url,key=result['api_base'],store.get('emby_key');headers={'X-Emby-Token':key}
                emby.update(connected=True,name=result['server'])
                async def safe(path,params=None):
                    try:
                        r=await emby_request(app.state.http,'GET',url+path,headers=headers,params=params,timeout=6);r.raise_for_status();return r.json()
                    except (httpx.HTTPError,ValueError) as exc:
                        errors.append(path+'：'+error_message(exc));return None
                async def optional(path,params=None):
                    # 可选数据源（活动日志等）：旧版 Emby 没有该接口时静默降级，不算连接错误。
                    try:
                        r=await emby_request(app.state.http,'GET',url+path,headers=headers,params=params,timeout=6);r.raise_for_status();return r.json()
                    except (httpx.HTTPError,ValueError):
                        return None
                counts,sessions,items=await asyncio.gather(safe('/Items/Counts'),safe('/Sessions'),safe('/Items',{'Recursive':'true','IncludeItemTypes':'Movie,Episode','SortBy':'DateCreated','SortOrder':'Descending','Limit':48,'Fields':'DateCreated,SeriesName,SeriesId,ImageTags,SeriesPrimaryImageTag,ProviderIds'}))
                emby['counts']=counts if isinstance(counts,dict) else {}
                emby['playing']=[{'name':v['NowPlayingItem'].get('Name',''),'series':v['NowPlayingItem'].get('SeriesName',''),'client':v.get('Client',''),'user':v.get('UserName',''),'paused':v.get('PlayState',{}).get('IsPaused',False)} for v in (sessions if isinstance(sessions,list) else []) if isinstance(v,dict) and isinstance(v.get('NowPlayingItem'),dict)]
                # 系列条目的 TMDB 编号属于剧集而非单集；批量读取，避免逐项请求。
                series_ids = sorted({str(i.get('SeriesId')) for i in (items.get('Items',[]) if isinstance(items,dict) else []) if isinstance(i,dict) and i.get('Type')=='Episode' and i.get('SeriesId')})
                series_data = await optional('/Items', {'Ids': ','.join(series_ids), 'Fields':'ProviderIds'}) if series_ids else None
                series_tmdb = {str(i.get('Id')): str((i.get('ProviderIds') or {}).get('Tmdb') or '') for i in (series_data.get('Items',[]) if isinstance(series_data,dict) else []) if isinstance(i,dict)}
                recent=[]
                seen_series=set()
                for i in (items.get('Items',[]) if isinstance(items,dict) else []):
                    if not isinstance(i,dict):continue
                    kind=i.get('Type');series_id=i.get('SeriesId')
                    own=i.get('Id') if i.get('ImageTags',{}).get('Primary') else None
                    if kind=='Episode' and series_id:
                        # 分集自己的图是 16:9 剧照，放进 2:3 的海报位会被裁得没法看；
                        # 同一部剧连播多集还会占满整行。按剧集去重，用剧集竖版海报。
                        if series_id in seen_series:continue
                        seen_series.add(series_id)
                        image_id=series_id if i.get('SeriesPrimaryImageTag') else own
                        recent.append({'id':i.get('Id'),'name':i.get('SeriesName') or i.get('Name'),
                                       'episode':i.get('Name'),'year':i.get('ProductionYear'),
                                       'kind':'Series','image_id':image_id,'tmdb_id':series_tmdb.get(str(series_id),'')})
                    else:
                        recent.append({'id':i.get('Id'),'name':i.get('Name'),'episode':None,
                                       'year':i.get('ProductionYear'),'kind':kind,'image_id':own, 'tmdb_id':str((i.get('ProviderIds') or {}).get('Tmdb') or '') if kind=='Movie' else ''})
                    if len(recent)>=12:break
                emby['recent']=recent
                # 观看记录与趋势取 Emby 活动日志：Type 是 playback.start，与界面语言无关。
                activity=await optional('/System/ActivityLog/Entries',{'StartIndex':0,'Limit':1000})
                starts=[]
                for row in (activity.get('Items',[]) if isinstance(activity,dict) else []):
                    if not isinstance(row,dict) or row.get('Type')!='playback.start':continue
                    at=activity_time(row.get('Date'))
                    if at:starts.append({'name':row.get('Name') or '','at':at,'item':row.get('ItemId')})
                emby['history']=starts[:8]
                day=datetime.date.today()
                # 只按实际取到的范围画：日志翻不到 14 天时不能把更早的日期画成 0。
                window=14
                if starts:
                    span=(day-datetime.date.fromtimestamp(min(row['at'] for row in starts))).days+1
                    window=max(1,min(14,span))
                labels=[(day-datetime.timedelta(days=offset)).strftime('%m-%d') for offset in range(window-1,-1,-1)]
                bucket={}
                for row in starts:
                    label=time.strftime('%m-%d',time.localtime(row['at']))
                    bucket[label]=bucket.get(label,0)+1
                emby['trend']=[{'date':label,'count':bucket.get(label,0)} for label in labels]
                emby['trend_days']=window
                emby['checked_at']=time.time()
                if not errors:app.state.emby_last_success=(fingerprint(store.get('emby_url'),key),dict(emby))
                elif not emby['counts'] and not emby['recent']:
                    # 子请求超时（例如 Emby 刚启动或正在扫描）时不能把总览清空：
                    # 只有连同库数量与最近入库都拿不到，才回退到上次成功快照并标记 stale。
                    prior=getattr(app.state,'emby_last_success',None)
                    if prior and prior[0]==fingerprint(store.get('emby_url'),store.get('emby_key')):
                        emby.update(prior[1]);emby.update(connected=False,playing=[],stale=True)
            except ValueError as exc:
                errors.append(str(exc))
                cached=getattr(app.state,'emby_last_success',None)
                if cached and cached[0]==fingerprint(store.get('emby_url'),store.get('emby_key')):
                    emby.update(cached[1]);emby.update(connected=False,playing=[],stale=True)
        else:errors.append('请先配置 Emby 服务地址和 API Key。')
        emby['error']='；'.join(errors)
        usage=shutil.disk_usage(app.state.strms)
        now=time.time()
        midnight=datetime.datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        by_state={r[0]:r[1] for r in store.db.execute('SELECT state,count(*) FROM organize_jobs GROUP BY state')}
        active=by_state.get('queued',0)+by_state.get('running',0)
        stats={'today':store.db.execute('SELECT count(*) FROM organize_jobs WHERE CAST(created AS REAL)>=?',(midnight,)).fetchone()[0],
               'week':store.db.execute('SELECT count(*) FROM organize_jobs WHERE CAST(created AS REAL)>=?',(now-7*86400,)).fetchone()[0],
               'total':sum(by_state.values()),
               'completed':by_state.get('completed',0),
               'partial':by_state.get('partial',0),
               'active':active,
               'last_at':store.db.execute('SELECT max(CAST(updated AS REAL)) FROM organize_jobs').fetchone()[0],
               'files':store.db.execute('SELECT count(*) FROM organized_files').fetchone()[0],
               'unread':store.db.execute('SELECT count(*) FROM notifications WHERE read_at IS NULL').fetchone()[0]}
        payload={'local':local,'recent':recent,'emby':emby,'stats':stats,'system':{'version':app.version,'free_bytes':usage.free,'total_bytes':usage.total,'used_bytes':usage.used,'115_configured':bool(store.get('cookie')),'tmdb_configured':bool(store.get('tmdb_key')),'active_tasks':active,'scheduler_ok':not engine.scheduler_fault,'strm_root':str(app.state.strms),'loop_lag':store.meta_json('loop_lag',{})}}
        return payload

    @app.get('/api/v1/cloud/account',dependencies=[Depends(admin)])
    async def cloud_account(refresh:int=Query(0,ge=0,le=1)):
        """115 账号与容量：10 分钟内复用缓存，refresh=1 立即重取。只读。"""
        cached=getattr(app.state,'cloud_account',None)
        if refresh or not cached or time.time()-cached.get('at',0)>600:
            try:
                cached={'at':time.time(),**await app.state.organizer.cloud.account()}
            except Exception as exc:
                cached={'at':time.time(),'error':str(exc)[:200] or '读取 115 账号信息失败'}
            app.state.cloud_account=cached
        return {k:v for k,v in cached.items() if k!='at'}
    @app.get('/api/v1/bot/commands',dependencies=[Depends(admin)])
    async def bot_commands_get():
        from .telegram_bot import BOT_COMMANDS, ordered_bot_commands
        saved=store.get('bot_commands_config',{}) or {}
        return {'commands':[{'name':name,'label':BOT_COMMANDS[name],'enabled':bool(saved.get(name,True))}
                           for name in ordered_bot_commands(store)]}

    @app.put('/api/v1/bot/commands',dependencies=[Depends(admin)])
    async def bot_commands_put(body:BotCommands):
        from .telegram_bot import BOT_COMMANDS
        unknown=[name for name in body.enabled if name not in BOT_COMMANDS]
        if unknown:
            raise HTTPException(422,'未知指令：'+'、'.join(unknown))
        if body.order is not None and (len(body.order)!=len(set(body.order)) or any(name not in BOT_COMMANDS for name in body.order)):
            raise HTTPException(422,'指令顺序包含重复或未知指令。')
        # 只存关掉的那些：未来新增指令默认可用，不用回头补配置。
        changes={'bot_commands_config':{name:value for name,value in body.enabled.items() if not value}}
        if body.order is not None:changes['bot_commands_order']=body.order
        store.set_many(changes)
        return await bot_commands_get()
    @app.get('/api/v1/cloud/maintenance',dependencies=[Depends(admin)])
    async def cloud_maintenance():
        config={**CloudMaintenance().model_dump(),**(store.get('cloud_maintenance_config',{}) or {})}
        return {'config':config,'last_sign_at':store.get('cloud_sign_last',0),
                'last_clear_at':store.get('cloud_recycle_last',0),
                'sign_due':store.get('cloud_sign_due',0),'recycle_due':store.get('cloud_recycle_due',0)}

    @app.put('/api/v1/cloud/maintenance',dependencies=[Depends(admin)])
    async def save_cloud_maintenance(body:CloudMaintenance):
        from croniter import croniter
        for label,value in (('签到',body.sign_cron),('回收站清空',body.recycle_cron)):
            if value and not croniter.is_valid(value):
                raise HTTPException(422,'%s的 Cron 表达式无效：%s' % (label,value))
        # 改完设置重算下次执行，否则旧的 due 会先跑一次。
        store.set_many({'cloud_maintenance_config':body.model_dump(),'cloud_sign_due':0,'cloud_recycle_due':0})
        return await cloud_maintenance()

    @app.post('/api/v1/cloud/sign',dependencies=[Depends(admin)])
    async def cloud_sign():
        result=await app.state.organizer.cloud.sign()
        store.set_many({'cloud_sign_last':time.time()})
        store.event('cloud','115 签到：'+str(result.get('message') or '完成'))
        days = result.get('days')
        date_text = datetime.datetime.now(ZoneInfo(store.get('timezone') or 'Asia/Shanghai')).strftime('%Y-%m-%d')
        heading, body = cloud_sign_notice(result.get('signed'), days, date_text, result.get('reward'))
        await app.state.organizer.notifications.publish(
            'cloud:sign:%d' % int(time.time()), heading, body,
            'success' if result.get('signed') else 'info', category='cloud')
        return result

    @app.get('/api/v1/cloud/recycle',dependencies=[Depends(admin)])
    async def cloud_recycle():
        try:
            info=await app.state.organizer.cloud.recycle_info()
        except Exception as exc:
            return {'available':False,'error':str(exc)[:200] or '读取回收站失败'}
        return {'available':True,'info':info if isinstance(info,dict) else {}}

    @app.post('/api/v1/cloud/recycle/clear',dependencies=[Depends(admin)])
    async def cloud_recycle_clear():
        config={**CloudMaintenance().model_dump(),**(store.get('cloud_maintenance_config',{}) or {})}
        before = None
        try:
            before = (await app.state.organizer.cloud.recycle_info() or {}).get('count')
        except Exception:
            before = None
        await app.state.organizer.cloud.recycle_clear(str(config.get('recycle_password') or '000000'))
        store.set_many({'cloud_recycle_last':time.time()})
        store.event('cloud','已清空 115 回收站。')
        when_text = datetime.datetime.now(ZoneInfo(store.get('timezone') or 'Asia/Shanghai')).strftime('%m/%d %H:%M')
        heading, body = cloud_recycle_notice(before, when_text)
        await app.state.organizer.notifications.publish(
            'cloud:recycle:%d' % int(time.time()), heading, body, 'success', category='cloud')
        return {'message':'回收站已清空。'}

    @app.get('/api/v1/dashboard/tmdb-poster/{kind}/{tmdb_id}',dependencies=[Depends(admin)])
    async def dashboard_tmdb_poster(kind: str, tmdb_id: int):
        # 根据 Emby 提供的 TMDB 编号查封面，不按标题猜测身份。缓存限制请求量。
        if kind not in ('movie','tv') or tmdb_id <= 0: raise HTTPException(404)
        key=store.get('tmdb_key')
        if not key: raise HTTPException(404)
        cache=getattr(app.state,'dashboard_poster_cache',None)
        if cache is None: cache=app.state.dashboard_poster_cache={}
        cache_key=(kind,tmdb_id); now=time.time(); entry=cache.get(cache_key)
        if not entry or entry[0]<now:
            # 进程内缓存只活到下次重启：再落一份 meta，重启后不用为同一批封面重新等 TMDB
            # （现场：一次重启后整页海报各慢 1.7~2.7 秒，日志里全是慢请求）。
            remembered=(store.meta_json('dashboard_posters',{}) or {}).get('%s:%s'%cache_key)
            if (isinstance(remembered,list) and len(remembered)==2 and isinstance(remembered[1],str)
                    and isinstance(remembered[0],(int,float)) and remembered[0]>now):
                entry=(float(remembered[0]),remembered[1]); cache[cache_key]=entry
        if not entry or entry[0]<now:
            try:
                r=await app.state.http.get(f'https://api.themoviedb.org/3/{kind}/{tmdb_id}',params={'api_key':key,'language':'zh-CN'},timeout=8)
                r.raise_for_status(); path=r.json().get('poster_path') or ''
                if not isinstance(path,str) or not path.startswith('/') or '/' in path[1:]: path=''
            except (httpx.HTTPError,ValueError,AttributeError): path=''
            if len(cache)>500: cache.clear()
            entry=(now+(21600 if path else 300),path);cache[cache_key]=entry
            saved={key:value for key,value in (store.meta_json('dashboard_posters',{}) or {}).items()
                   if isinstance(value,list) and len(value)==2 and isinstance(value[0],(int,float))
                   and value[0]>now}
            saved['%s:%s'%cache_key]=[entry[0],entry[1]]
            if len(saved)>2000:
                saved=dict(sorted(saved.items(),key=lambda item:item[1][0],reverse=True)[:2000])
            store.set_meta('dashboard_posters',saved)
        if not entry[1]: raise HTTPException(404)
        return RedirectResponse('https://image.tmdb.org/t/p/w500'+entry[1],headers={'Cache-Control':'private,max-age=3600'})
    @app.get('/api/v1/dashboard/image/{item_id}',dependencies=[Depends(admin)])
    async def image(item_id:str):
        if not item_id.isalnum():raise HTTPException(422,'无效媒体编号。')
        connection=app.state.organizer.emby.connection()
        if not connection:raise HTTPException(404)
        url,key=connection
        r=await emby_request(app.state.http,'GET',url+'/Items/'+item_id+'/Images/Primary',headers={'X-Emby-Token':key},params={'MaxWidth':400,'Quality':85},timeout=10)
        if r.status_code!=200:raise HTTPException(404)
        return Response(r.content,media_type=r.headers.get('content-type','image/jpeg'),headers={'Cache-Control':'private,max-age=300'})
