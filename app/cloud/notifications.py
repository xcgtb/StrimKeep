"""Confirmed arrivals, grouped per publication batch and season."""
import hashlib
import html
import json
import time
from pathlib import PurePosixPath
from app.cloud import store, settings, events


def group_key(asset, connection, revision, origin):
    m=asset['payload']['media']
    return hashlib.sha256(store.encode([origin or asset['id'],connection,revision,m['kind'],str(m['tmdb_id']),str(m.get('season','')),asset['payload'].get('source_kind','personal')]).encode()).hexdigest()


def caption(assets, connection):
    m=assets[0]['payload']['media']
    def safe(v):return html.escape(str(v))
    episodes=sorted({int(a['payload']['media']['episode']) for a in assets if a['payload']['media'].get('episode') is not None})
    ranges=[]
    for n in episodes:
        if ranges and n==ranges[-1][1]+1:ranges[-1][1]=n
        else:ranges.append([n,n])
    suffix=''
    if m['kind']=='tv':
        suffix=' S'+str(int(m['season'])).zfill(2)+' '+', '.join('E'+str(a).zfill(2)+(('-E'+str(b).zfill(2)) if b!=a else '') for a,b in ranges)
    source='115网盘分享（分享库 · 免转存）' if assets[0]['payload'].get('source_kind')=='share' else '115网盘'
    versions=sorted({' '.join(str(a['payload']['media'].get('naming_fields',{}).get(k) or a['payload']['media'].get(k) or '') for k in ('source','dynamic_range','resolution')).strip() for a in assets})
    groups=sorted({str(a['payload']['media'].get('naming_fields',{}).get('release_group') or '-') for a in assets})
    size=sum(int(a['payload']['source'].get('size') or 0) for a in assets)
    relative=PurePosixPath(assets[0]['local_path']).relative_to(PurePosixPath(assets[0]['payload']['output_root']))
    # Exclude the title/season/file suffix; only report known category components.
    category='/'.join(relative.parts[:-3] if m['kind']=='tv' else relative.parts[:-2]) or '未分类'
    text='✅ Emby 已确认入库\n📺 '+safe(m['title'])+' ('+safe(m.get('year',''))+')'+safe(suffix)
    text+='\n☁️ 来源：'+source+'\n🗂 分类：'+safe(category)+'\n🎞 版本：'+safe(' / '.join(versions) or '未识别')
    text+='\n📁 文件：'+str(len(assets))+' 个'+(('\n🎬 本次：'+str(len(episodes))+' 集') if episodes else '')
    text+='\n💾 体积：'+format(size/1024**3,'.2f')+' GB\n🎟 影号：TMDB '+safe(m['tmdb_id'])+'\n👥 组名：'+safe(' / '.join(groups))+'\n媒体库：'+safe(connection['name'])
    return text


def send_confirmed(group, connection):
    if not group:return
    if not connection['notify'] or not settings.get()['notify_enabled'] or not settings.get()['notify_strm']:
        with store.db() as con:con.execute("INSERT OR IGNORE INTO cloud_ingestion_notifications VALUES(?,'disabled',?)",(group,time.time()))
        return
    with store.db() as con:
        rows=con.execute("SELECT id,asset_id,state,payload FROM cloud_ingestion WHERE connection_id=? AND json_extract(payload,'$.notification_group')=?",(connection['id'],group)).fetchall()
        if not rows or any(r[2]=='pending' for r in rows):return
        origin=json.loads(rows[0][3]).get('origin_job','')
        if origin:
            job=con.execute('SELECT state FROM cloud_jobs WHERE id=?',(origin,)).fetchone()
            due=con.execute("SELECT 1 FROM cloud_job_due d JOIN cloud_jobs j ON j.id=d.job_id WHERE d.origin_job=? AND j.state IN ('queued','running')",(origin,)).fetchone()
            if (job and job[0] in ('queued','running')) or due:return
        ids={r[1]:json.loads(r[3]) for r in rows if r[2]=='confirmed'}
        if not ids:return
        assets=[]
        for aid,bound in ids.items():
            asset=store.asset(aid)
            if asset and asset['state']=='active' and asset['payload']['digest']==bound['digest'] and asset['local_path']==bound['local_path']:assets.append(asset)
        if not assets:
            con.execute("INSERT OR IGNORE INTO cloud_ingestion_notifications VALUES(?,'stale',?)",(group,time.time()))
            return
        # Claim before remote send; an ambiguous send is never retried automatically.
        changed=con.execute("INSERT OR IGNORE INTO cloud_ingestion_notifications VALUES(?,'sending',?)",(group,time.time())).rowcount
        if not changed:return
    from app.tg import notify_telegram_card
    sent=False
    try:
        m=assets[0]['payload']['media']
        poster=m.get('backdrop_path') or m.get('poster_path')
        sent=bool(notify_telegram_card(caption(assets,connection),'https://image.tmdb.org/t/p/w500'+poster if poster else ''))
    except Exception:
        pass
    result='已发送' if sent else '发送失败或结果未知，未自动重发'
    with store.db() as con:
        con.execute('UPDATE cloud_ingestion_notifications SET state=?,updated=? WHERE id=?',('sent' if sent else 'unknown',time.time(),group))
        for row in rows:
            if row[2]=='confirmed':
                value=json.loads(con.execute('SELECT result FROM cloud_ingestion WHERE id=?',(row[0],)).fetchone()[0]);value['notification']=result
                con.execute('UPDATE cloud_ingestion SET result=? WHERE id=?',(store.encode(value),row[0]))
    events.emit('notify','Emby 入库海报通知：'+result,level='INFO' if sent else 'WARNING')
