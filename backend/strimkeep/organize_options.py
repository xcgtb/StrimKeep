"""Saved organization options, normalized once and captured by queued tasks."""
import re
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator
from .media import METADATA,VIDEOS,SUBTITLES

# 只剩「完全推不出 115 目录名」时才会用到的兜底值（0.8.30 起默认镜像 115 目录名）。
DEFAULT_LIBRARY_DIR_NAME='影视媒体库'


def cloud_dir_name(label):
    """把「115 网盘 / 媒体库」这类面包屑标签取成最后一段目录名。"""
    text=str(label or '').strip()
    for separator in (' / ','/','\\'):
        if separator in text:return text.rsplit(separator,1)[-1].strip()
    return text


def library_root(config,path=None):
    """本地 STRM 的第一段目录名（0.8.30：默认镜像 115 上的目录名）。

    为什么不再用固定名字：STRM 是 115 目录的映射。以前本地固定叫「影视媒体库」，
    网盘上叫「媒体库」，于是本地树、台账 relative_path、Emby 库根、磁盘目录四处
    各写各的：台账被按配置改到「影视媒体库」，文件却还在「媒体库」，978 行台账里
    924 行在磁盘上找不到，Emby 也只能收录一半。

    取值顺序：
    1. `library_dir_name` 显式填写（兼容老配置，也允许本地想用别的名字）；
    2. `path`（扫描到的 115 路径 `[{'id','name'},...]`）里归档目录的真实名字；
    3. `directory_labels[archive_cid]` 的最后一段（设置页保存的面包屑标签）；
    4. `DEFAULT_LIBRARY_DIR_NAME`。
    """
    config=config or {}
    name=str(config.get('library_dir_name') or '').strip()
    if name:return name
    archive=str(config.get('archive_cid') or '')
    for entry in (path or []):
        if str(entry.get('id'))==archive:
            label=str(entry.get('name') or '').strip()
            if label:return label
    if archive:
        label=cloud_dir_name((config.get('directory_labels') or {}).get(archive))
        if label:return label
    return DEFAULT_LIBRARY_DIR_NAME


class OrganizeOptions(BaseModel):
    model_config=ConfigDict(extra='ignore')
    video_extensions:list[str]=Field(default_factory=lambda:sorted(VIDEOS),max_length=40)
    subtitle_extensions:list[str]=Field(default_factory=lambda:sorted(SUBTITLES),max_length=20)
    min_video_size_mb:float=Field(default=0,ge=0,le=1048576)
    recycle_small_videos:bool=False
    # 直接回收（可恢复）。NFO、海报等元数据不在此列——它们要留给 Emby。
    # 关掉则退回原来的「转待处理」。
    discard_inferior:bool=True
    # 本项目「未识别整包保留附属文件、非视频内容阻止空目录清理」是既有契约，
    recycle_small_files:bool=False
    recycle_small_threshold_mb:float=Field(default=100,ge=0,le=1048576)
    ignore_keywords:list[str]=Field(default_factory=list,max_length=100)
    # 本地 STRM 的库根目录名（第一段）。0.8.30 起**留空 = 镜像 115 目录名**：
    # 归档目录在网盘上叫「媒体库」，本地就写「媒体库」，四处（本地树/台账/Emby/磁盘）
    # 才能天然对齐。填了值才用它覆盖（老配置与「本地想换名字」仍旧有效）。
    library_dir_name:str=''
    rename_enabled:bool=True
    keep_existing_names:bool=True
    folder_language:Literal['zh','en']='zh'
    filename_language:Literal['zh','en']='zh'
    ffprobe_enabled:bool=True
    probe_prefetch:bool=True
    identify_prefetch:bool=True
    probe_concurrency:int=Field(default=2,ge=1,le=8)
    classify_enabled:bool=True
    wash_enabled:bool=True
    children_enabled:bool=True
    concert_enabled:bool=True
    region_enabled:bool=True
    year_enabled:bool=False
    cleanup_empty_enabled:bool=True

    @field_validator('video_extensions','subtitle_extensions',mode='before')
    @classmethod
    def extensions(cls,value):
        if isinstance(value,str):value=value.split(',')
        result=[]
        for raw in value:
            v=str(raw).strip().lower().lstrip('.')
            if not re.fullmatch('[a-z0-9]{1,10}',v):raise ValueError('扩展名只能使用1至10位字母或数字')
            if '.'+v not in result:result.append('.'+v)
        return result

    @field_validator('ignore_keywords',mode='before')
    @classmethod
    def keywords(cls,value):
        if isinstance(value,str):value=value.split(',')
        return list(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))

    @model_validator(mode='after')
    def valid(self):
        if set(self.video_extensions)&set(self.subtitle_extensions):raise ValueError('视频和字幕扩展名不能重复')
        if any(v not in SUBTITLES for v in self.subtitle_extensions):raise ValueError('当前支持的字幕类型：'+', '.join(sorted(SUBTITLES)))
        if any(len(v)>100 for v in self.ignore_keywords):raise ValueError('忽略关键词最长100字')
        name=str(self.library_dir_name or '').strip()
        if name and (len(name)>60 or any(c in name for c in '/\\:\0') or name in ('.','..')):
            raise ValueError('库目录名只能是单层目录名，最长60字；留空表示镜像 115 目录名')
        self.library_dir_name=name
        return self


def options(config):return OrganizeOptions.model_validate(config or {}).model_dump()


def filter_files(files,config):
    cfg=options(config);allowed=set(cfg['video_extensions']);subs=set(cfg['subtitle_extensions'])
    accepted=[];report={'scanned':len(files),'excluded':0,'attachments':0,'formats':{},'examples':[],
                       'examples_truncated':False,'allowed_extensions':sorted(allowed),'reasons':{},
                       'recycle':[]}
    for entry in files:
        ext=Path(entry['name']).suffix.lower();reason=''
        if ext in subs:
            accepted.append(entry);report['attachments']+=1;continue
        if ext not in allowed:
            small=float(entry.get('size') or 0)<cfg['recycle_small_threshold_mb']*1024*1024
            if (cfg['recycle_small_files'] and ext not in METADATA and small
                    and str(entry.get('parent_id') or '').isdigit()):
                report['recycle'].append({'id':entry['id'],'parent_id':entry['parent_id'],
                                          'name':entry['name'],'size':entry.get('size') or 0})
                reason='非整理类型且小于最小体积阈值 %gMB，已回收' % cfg['recycle_small_threshold_mb']
            else:
                reason='未列入当前视频或字幕扩展名，按规则跳过'
        elif any(k.casefold() in entry['name'].casefold() for k in cfg['ignore_keywords']):
            reason='命中忽略关键词，保留在源目录'
        elif cfg['min_video_size_mb'] and float(entry.get('size') or 0)<cfg['min_video_size_mb']*1024*1024:
            if cfg['recycle_small_videos'] and str(entry.get('parent_id') or '').isdigit():
                report['recycle'].append({'id':entry['id'],'parent_id':entry['parent_id'],
                                          'name':entry['name'],'size':entry.get('size') or 0})
                reason=f"低于最小视频大小 {cfg['min_video_size_mb']:g} MB，已回收"
            else:
                reason=f"低于最小视频大小 {cfg['min_video_size_mb']:g} MB，保留在源目录"
        if reason:
            report['excluded']+=1;label=ext or '无扩展名'
            report['formats'][label]=report['formats'].get(label,0)+1
            report['reasons'][reason]=report['reasons'].get(reason,0)+1
            if len(report['examples'])<50:report['examples'].append({'name':entry['name'],'extension':ext,'reason':reason})
        else:accepted.append(entry)
    report['examples_truncated']=report['excluded']>len(report['examples'])
    return accepted,report


def file_title(name):
    match=re.match(r'^(.*?)[ ._-]+\(?\d{4}\)?(?:[ ._-]|$)',Path(name).stem)
    return match[1].strip(' ._-') if match else ''


async def title_options(engine,hint,details,config):
    cfg=options(config);kind=hint['media_type'];original=details.get('original_title') or details.get('original_name') or ''
    chinese=details.get('title') or details.get('name') or original
    cjk=lambda x:bool(re.search('[\u3400-\u9fff]',x))
    if not cjk(chinese) and cjk(original):chinese=original
    if not cjk(chinese) and 'zh' in (cfg['folder_language'],cfg['filename_language']):
        try:
            alt=await engine.tmdb(f"{kind}/{details['id']}/alternative_titles")
            rows=alt.get('titles',alt.get('results',[]))
            for region in ('CN','TW','HK'):
                found=next((v.get('title') for v in rows if v.get('iso_3166_1')==region and cjk(v.get('title',''))),None)
                if found:chinese=found;break
        except Exception:pass
    english=original if details.get('original_language')=='en' else ''
    if 'en' in (cfg['folder_language'],cfg['filename_language']):
        translated=await engine.tmdb(f"{kind}/{details['id']}",{'language':'en-US'})
        english=translated.get('title') or translated.get('name') or english or original or chinese
    return {**cfg,'folder_title':chinese if cfg['folder_language']=='zh' else english,
            'file_title':chinese if cfg['filename_language']=='zh' else english}
