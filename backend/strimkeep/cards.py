"""Telegram media cards: presentation only, no filesystem or scan state."""
import re
from pathlib import Path

from .media import release_group


def line(value,limit=90):
    text=' '.join(str(value or '').split())
    return text if len(text)<=limit else text[:limit-1]+'…'


def episode_text(numbers):
    """把集号压成区间：E01-E26、E27；有断层就分段，不逐集铺开。"""
    ranges=[]
    for n in sorted({n for n in numbers if isinstance(n,int) and not isinstance(n,bool)}):
        if ranges and n==ranges[-1][-1]+1:ranges[-1].append(n)
        else:ranges.append([n])
    return '、'.join(f'E{x[0]:02d}'+(f'-E{x[-1]:02d}' if len(x)>1 else '') for x in ranges)


def episode_range(items):
    episodes=set()
    for _,plan in items:
        hint=plan.get('hint',{})
        start=hint.get('episode')
        if isinstance(start,int):
            end=hint.get('end_episode')
            if not isinstance(end,int):end=start
            episodes.update(range(start,min(end,start+10000)+1))
    return episode_text(episodes)


def outcome_lines(items, season, unresolved=0):
    """仅描述本批实际状态，不把未列出的集数推断为缺集。

    保留已有 / 跳过都是洗版结论：文件被主动留下，不是入库失败，所以不再写成
    「未入库」。集号读得出来就压成区间（E01-E26、E27），读不出来（例如
    S01E16-1080P.mkv 这种缺作品名的名字）只报数量，不逐条铺开。
    """
    from .media import parse_name
    buckets = {'保留已有': [], '洗版处理': [], '其他跳过': [], '待核查': []}
    untagged = {label: 0 for label in buckets}
    unknown = unresolved
    for source, plan, state, step in items:
        step = str(step or '')
        if state in ('pending','review'): label = '待核查'
        elif state == 'recycled' or ('洗版' in step and '回收' in step): label = '洗版处理'
        elif '已有' in step and ('保留' in step or '跳过' in step): label = '保留已有'
        else: label = '其他跳过'
        try:
            hint = plan.get('hint') or parse_name(source.get('name',''), source.get('folder'))
            start = hint.get('episode'); end = hint.get('end_episode') or start
            if (hint.get('media_type') != 'tv' or hint.get('season') != season
                    or not isinstance(start,int) or not isinstance(end,int)
                    or end < start or end-start > 100):
                raise ValueError('episode')
        except Exception:
            untagged[label] += 1
            continue
        buckets[label].extend(range(start,end+1))
    lines=[]
    for label,values in buckets.items():
        text=episode_text(values)
        if untagged[label]:
            text=(text+f'（另 {untagged[label]} 项无集号）') if text else f'{untagged[label]} 项（无集号）'
        if text: lines.append(f'📌 {label}：'+line(text,100))
    if unknown:lines.append(f'ℹ️ 本批另有 {unknown} 项无法归到本季，请查看任务明细')
    return lines


def version(source,media):
    name=source.get('name','')
    match=re.search(r'(?i)(?<![a-z])(?:WEB[ ._-]?DL|WEBRIP|BLU[ ._-]?RAY|BDREMUX|REMUX|HDTV)(?![a-z])',name)
    remux=re.search(r'(?i)(?<![a-z])(?:BD[ ._-]?)?REMUX(?![a-z])',name)
    source_type='REMUX' if remux else (re.sub(r'[ ._]','-',match[0].upper()) if match else '')
    dynamic={'Dolby Vision':'DoVi','Dolby Vision HDR':'DoVi HDR','HDR10+':'HDR10+','HDR10':'HDR10','HDR PQ':'HDR','HLG':'HLG','SDR':'SDR'}.get(media.get('dynamic_range'),'')
    width,height=media.get('width') or 0,media.get('height') or 0
    width,height=max(width,height),min(width,height)
    resolution='4320p' if width>=7000 or height>=4000 else '2160p' if width>=3500 or height>=2000 else '1440p' if width>=2500 or height>=1400 else '1080p' if width>=1800 or height>=1000 else '720p' if width>=1200 or height>=700 else f'{height}p' if height else ''
    return '.'.join(x for x in (source_type,dynamic) if x)+(' ' if resolution and (source_type or dynamic) else '')+resolution


def media_card(items,shared=False,transferred=False,emby_status='',other_items=(),unassigned=0):
    source,plan=items[0];details=plan.get('details',{});hint=plan.get('hint',{})
    title=line(details.get('title') or details.get('name') or '未命名作品',100)
    year=str(details.get('release_date') or details.get('first_air_date') or '')[:4]
    heading=('🎉 新片入库：' if emby_status in ('verified','pending') else '🎬 整理完成：')+title+(f' ({year})' if year.isdigit() and len(year)==4 else '')
    lines=[];episodes=episode_range(items);season=hint.get('season')
    if episodes:lines.append('📺 本批：'+(f'S{season:02d} ' if isinstance(season,int) else '')+episodes)
    if hint.get('media_type')=='tv':
        replaced=[(src,p) for src,p in items if p.get('share_wash_replaced') or (p.get('wash') or {}).get('decision')=='replace']
        if replaced:
            washed=episode_range(replaced)
            if washed:lines.append('🔄 洗版：'+line(washed,100)+'（替换旧版）')
        lines.extend(outcome_lines(other_items,season,unassigned))
    versions=list(dict.fromkeys(version(s,p.get('media',{})) for s,p in items))
    versions=[v for v in versions if v]
    specification=line(' / '.join(versions[:3])+(' 等版本' if len(versions)>3 else ''),130)
    category=' / '.join(plan.get('parts',[])[:2])
    if specification:lines.append('🎞️ 画质：'+specification)
    if category:lines.append('🗂️ 分类：'+line(category))
    # 转存后文件已经在自己网盘里，与网盘原有文件同归「115网盘」；
    # 只有免转存的分享 STRM 才单独标「115网盘分享」。transferred 仍保留在签名里，
    # 供调用方继续区分批次来源（留档、排查用），不再影响这张卡片的文案。
    lines.append('🔗 来源：115网盘分享' if shared else '☁️ 来源：115网盘')
    lines.append(f'📁 文件：{len(items)} 个')
    if hint.get('media_type')=='tv' and isinstance(season,int):
        total=next((s.get('episode_count') for s in details.get('seasons',[]) if s.get('season_number')==season),None)
        if isinstance(total,int) and total>0:lines.append(f'🗓️ 本季：S{season:02d} 共 {total} 集')
    sizes=[s.get('size') for s,_ in items]
    known=[v for v in sizes if isinstance(v,(int,float)) and not isinstance(v,bool) and v>0]
    size_text=(f'{sum(known)/1024**3:.2f}G'+('（部分未知）' if len(known)!=len(sizes) else '')) if known else '未知'
    lines.append('💾 体积：'+size_text)
    identity=details.get('id') or hint.get('tmdb_id')
    if identity:lines.append('🆔 编号：TMDB '+line(identity,30))
    groups=[]
    for s,_ in items:
        # 和命名共用同一套解析：`-SCDIY@HiveWeb` 取 HiveWeb，
        # 避免通知里的组别跟真正写进文件名的组别对不上。
        group=release_group(s.get('name',''))
        if group:groups.append(group)
    lines.append('👥 组别：'+(line(' / '.join(dict.fromkeys(groups)),80) or '未知'))
    if emby_status=='pending':lines.append('⏳ 状态：Emby 入库待核实')
    elif emby_status=='verified':lines.append('✅ 状态：Emby 已入库')
    elif emby_status=='unconfirmed':lines.append('⚠️ 状态：Emby 入库未确认，请检查媒体库')
    backdrop=details.get('backdrop_path');poster=backdrop or details.get('poster_path')
    photo=('https://image.tmdb.org/t/p/'+('w780' if backdrop else 'w500')+poster) if isinstance(poster,str) and re.fullmatch(r'/[A-Za-z0-9_-]+\.(?:jpg|png)',poster) else None
    # Telegram caption 上限按 UTF-16 计算；保留末尾状态，避免长片名截掉入库结果。
    def units(value):return len(value.encode('utf-16-le'))//2
    status_line=lines[-1] if emby_status else ''
    details=lines[:-1] if status_line else lines[:]
    while details and units(heading+'\n'+'\n'.join(details+[status_line] if status_line else details))>950:
        details.pop()
    if len(details)<len(lines)-(1 if status_line else 0):details.append('…其他信息见任务中心')
    if status_line:details.append(status_line)
    return heading,'\n'.join(details),photo
