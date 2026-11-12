import time
from typing import Literal
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from .offline_api import directory_config


class TransferConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    target_cid:str=Field(default='',max_length=24)
    target_label:str=Field(default='',max_length=500)
    bot_target_cid:str|None=Field(default=None,max_length=24)
    bot_target_label:str|None=Field(default=None,max_length=500)
    # 影视探索是独立入口：可以单独指定转存目录，留空就跟随上面的 Web 转存目录。
    explore_cid:str=Field(default='',max_length=24)
    explore_label:str=Field(default='',max_length=500)
    auto_organize:bool=False          # Web 转存：落盘后是否立即整理
    bot_auto_organize:bool=False      # Telegram 转存：同上（分开设置）
    bot_enabled:bool=False
    allowed_user_id:str=Field(default='',max_length=24)


class TransferInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(min_length=1,max_length=16000)
    target_cid:str=Field(default='',max_length=24)


class DestinationRow(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source:Literal['web','bot','channel']
    channel:str=Field(default='',max_length=64)
    share_mode:Literal['strm','transfer','none']='transfer'
    share_cid:str=Field(default='',max_length=24)
    share_label:str=Field(default='',max_length=500)
    magnet_cid:str=Field(default='',max_length=24)
    magnet_label:str=Field(default='',max_length=500)
    ed2k_cid:str=Field(default='',max_length=24)
    ed2k_label:str=Field(default='',max_length=500)


def install_transfer(app,store,admin,job_public):
    def engine():return app.state.organizer

    @app.get('/api/v1/transfer/config',dependencies=[Depends(admin)])
    async def config():
        value={**TransferConfig().model_dump(),**store.get('transfer_config',{})}
        if value.get('bot_target_cid') is None:
            value['bot_target_cid']=value['target_cid']
            value['bot_target_label']=value['target_label']
        chat=str(store.get('notification_config',{}).get('chat_id',''))
        if not value.get('allowed_user_id') and chat.isascii() and chat.isdigit() and int(chat)>0:value['allowed_user_id']=chat
        return {'config':value,'bot':store.get('bot_runtime',{'state':'disabled'})}

    @app.put('/api/v1/transfer/config',dependencies=[Depends(admin)])
    async def save(body:TransferConfig):
        if body.bot_target_cid is None:
            previous=store.get('transfer_config',{}) or {}
            body.bot_target_cid=previous.get('bot_target_cid') if previous.get('bot_target_cid') is not None else previous.get('target_cid','')
            body.bot_target_label=previous.get('bot_target_label') if previous.get('bot_target_cid') is not None else previous.get('target_label','')
        if body.target_cid:
            if not body.target_cid.isdigit() or int(body.target_cid)<=0:raise HTTPException(422,'请选择一个非根目录作为转存目标。')
            await app.state.cloud.directory(body.target_cid)
        if body.bot_target_cid is not None and body.bot_target_cid:
            if not body.bot_target_cid.isdigit() or int(body.bot_target_cid)<=0:raise HTTPException(422,'请选择一个非根目录作为机器人转存目标。')
            await app.state.cloud.directory(body.bot_target_cid)
        if body.explore_cid:
            if not body.explore_cid.isdigit() or int(body.explore_cid)<=0:raise HTTPException(422,'请选择一个非根目录作为影视探索转存目标。')
            await app.state.cloud.directory(body.explore_cid)
        if body.bot_enabled:
            n=store.get('notification_config',{})
            if not n.get('telegram_enabled') or not n.get('bot_token'):raise HTTPException(409,'请先在通知页启用 Telegram 并保存 Bot Token。')
            if not body.allowed_user_id.isdigit() or int(body.allowed_user_id)<=0:raise HTTPException(422,'请填写允许发送转存命令的 TG 用户数字 ID。')
        values={'transfer_config':body.model_dump()}
        if body.bot_enabled!=store.get('transfer_config',{}).get('bot_enabled',False):
            values.update(bot_epoch=str(time.time_ns()),bot_runtime={'state':'connecting' if body.bot_enabled else 'disabled'})
        store.set_many(values)
        return await config()

    @app.get('/api/v1/transfer/destinations',dependencies=[Depends(admin)])
    async def destinations():
        transfer=(await config())['config']
        offline=directory_config(store.get('offline_config',{}) or {})
        channels=engine().channels.config()['channels']
        def row(source, share_cid, share_label, magnet_cid, magnet_label, ed2k_cid, ed2k_label):
            return dict(source=source,channel='',share_mode='transfer',share_cid=share_cid or '',share_label=share_label or '',magnet_cid=magnet_cid or '',magnet_label=magnet_label or '',ed2k_cid=ed2k_cid or '',ed2k_label=ed2k_label or '')
        return {'rows':[
            row('web',transfer['target_cid'],transfer['target_label'],offline['magnet_cid'],offline['magnet_label'],offline['ed2k_cid'],offline['ed2k_label']),
            row('bot',transfer['bot_target_cid'],transfer['bot_target_label'],offline['bot_magnet_cid'],offline['bot_magnet_label'],offline['bot_ed2k_cid'],offline['bot_ed2k_label']),
            *[dict(row('channel',c['share_cid'],c['share_label'],c['magnet_cid'],c['magnet_label'],c['ed2k_cid'],c['ed2k_label']),channel=c['name'],share_mode=c['share_mode']) for c in channels]]}

    @app.put('/api/v1/transfer/destinations',dependencies=[Depends(admin)])
    async def save_destinations(body:DestinationRow):
        ids={body.magnet_cid,body.ed2k_cid}
        if body.source!='channel' or body.share_mode=='transfer':ids.add(body.share_cid)
        for cid in ids-{''}:
            if not cid.isdigit() or int(cid)<=0:raise HTTPException(422,'请选择非根目录。')
            await app.state.cloud.directory(cid)
        if body.source=='channel':
            current=engine().channels.config()
            found=next((c for c in current['channels'] if c['name']==body.channel),None)
            if found is None:raise HTTPException(404,'频道不存在，请先添加频道。')
            if body.share_mode=='transfer' and not body.share_cid:raise HTTPException(422,'请为频道分享转存选择目录。')
            for field in ('share_mode','share_cid','share_label','magnet_cid','magnet_label','ed2k_cid','ed2k_label'):
                found[field]=getattr(body,field)
            engine().channels.save(current)
        else:
            transfer={**TransferConfig().model_dump(),**(store.get('transfer_config',{}) or {})}
            if transfer.get('bot_target_cid') is None:
                transfer['bot_target_cid']=transfer.get('target_cid','')
                transfer['bot_target_label']=transfer.get('target_label','')
            offline=directory_config(store.get('offline_config',{}) or {})
            prefix='' if body.source=='web' else 'bot_'
            transfer[prefix+'target_cid']=body.share_cid
            transfer[prefix+'target_label']=body.share_label
            for kind in ('magnet','ed2k'):
                for suffix in ('cid','label'):
                    offline[prefix+kind+'_'+suffix]=getattr(body,kind+'_'+suffix)
            offline['directories_v2']=True
            store.set_many({'transfer_config':transfer,'offline_config':offline})
        return await destinations()

    @app.post('/api/v1/transfer/jobs',dependencies=[Depends(admin)])
    async def start(body:TransferInput):
        config=store.get('transfer_config',{})
        try:ids=await engine().transfer.enqueue(body.text,body.target_cid or config.get('target_cid',''),config.get('auto_organize',False))
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        return {'items':[job_public(engine().job(jid)) for jid in ids]}

    @app.get('/api/v1/transfer/jobs',dependencies=[Depends(admin)])
    async def jobs():
        rows=store.db.execute("SELECT * FROM organize_jobs WHERE json_extract(config,'$.job_kind')='transfer' ORDER BY id DESC LIMIT 50").fetchall()
        return {'items':[job_public(row) for row in rows]}
