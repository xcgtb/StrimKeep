"""Recycle verified extensionless text companions after a video is archived."""
import json
import codecs
import re
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024


def kind(data):
    try:
        # A bounded HTTP range can end in the middle of a UTF-8 character.
        text = codecs.getincrementaldecoder('utf-8-sig')().decode(data, final=False)
    except UnicodeDecodeError:
        return ''
    lower = text.lower()
    if lower.lstrip().startswith('webvtt'):
        return 'WebVTT 字幕'
    if '[script info]' in lower and '[events]' in lower:
        return 'ASS 字幕'
    if re.search(r'\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}', text):
        return 'SRT 字幕'
    if (('<!doctype html' in lower or '<html' in lower) and 'themoviedb.org' in lower
            and 'the movie database' in lower and re.search(r'<title\b',lower)):
        return 'TMDB 网页'
    return ''


def candidates(files):
    return [{k:entry.get(k) for k in ('id','name','size','parent_id','sha1')}
            for entry in files if not Path(entry['name']).suffix and
            type(entry.get('size')) is int and 0 < entry['size'] <= MAX_BYTES and
            str(entry.get('parent_id') or '').isdigit() and str(entry.get('id') or '').isdigit()][:100]


async def recycle_companions(engine, job_id, config):
    candidates_list = config.get('untyped_companions') or []
    if not candidates_list:
        return 0
    done = engine.store.db.execute("SELECT original FROM organize_items WHERE job_id=? AND state='done'",(job_id,)).fetchall()
    parents = {str(json.loads(row['original']).get('parent_id') or '') for row in done}
    recycled = 0
    for entry in candidates_list:
        if entry['parent_id'] not in parents:
            continue
        engine.boundary(job_id)
        try:
            current = await engine.cloud.file(entry['id'])
            engine.check_source(entry,current,entry['name'],entry['parent_id'])
            ua = 'StrimKeep/companion-check'
            url,headers = await engine.cloud.url(current['pickcode'],ua)
            headers = {**headers,'User-Agent':ua,'Range':'bytes=0-8191'}
            async with engine.http.stream('GET',url,headers=headers,follow_redirects=True,timeout=15) as response:
                if response.status_code != 206:
                    continue
                data=bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part[:max(0,8192-len(data))])
                    if len(data)>=8192:break
            label = kind(bytes(data))
            if not label:
                continue
            current=await engine.cloud.file(entry['id'])
            engine.check_source(entry,current,entry['name'],entry['parent_id'])
            await engine.cloud.recycle(entry['id'],entry['parent_id'])
            recycled+=1
            report=config.get('scan_filter') or {}
            report['excluded']=max(0,report.get('excluded',0)-1)
            for key in ('formats','reasons'):
                name='无扩展名' if key=='formats' else '未列入当前视频或字幕扩展名，按规则跳过'
                if report.get(key,{}).get(name):
                    report[key][name]-=1
                    if not report[key][name]:del report[key][name]
            for i,example in enumerate(report.get('examples',[])):
                if example.get('name')==entry['name'] and not example.get('extension'):
                    report['examples'].pop(i);break
            engine.store.event('organize',f'任务 {job_id}：已回收已整理视频同目录的无扩展名{label}：{entry["name"]}。')
        except Exception as exc:
            engine.store.event('organize',f'任务 {job_id}：无扩展名附属文件未回收，保留原文件（{type(exc).__name__}）。')
    return recycled
