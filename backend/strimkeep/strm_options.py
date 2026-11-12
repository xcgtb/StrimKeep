"""One source of truth for STRM filters and shared effective settings."""
import hashlib,json,os,secrets
from pathlib import Path
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator
from .organize_options import OrganizeOptions,options
from .media import VIDEOS,SUBTITLES
from .quality import public_policy

class StrmOptions(BaseModel):
    model_config=ConfigDict(extra='ignore')
    inherit_video_extensions:bool=True
    video_extensions:list[str]=Field(default_factory=lambda:sorted(VIDEOS),max_length=60)
    min_video_size_mb:float=Field(default=0,ge=0,le=1048576)
    metadata_extensions:list[str]=Field(default_factory=lambda:sorted(SUBTITLES),max_length=60)
    generate_nfo:bool=False
    generate_images:bool=False
    emby_media_info:bool=False
    stale_detect:bool=False
    webhook_token:str=Field(default='',max_length=200)
    deep_delete_dry_run:bool=True
    cleanup_empty:bool=True

    @field_validator('video_extensions','metadata_extensions',mode='before')
    @classmethod
    def extensions(cls,value):return OrganizeOptions.extensions(value)

    @model_validator(mode='after')
    def valid(self):
        if '.strm' in self.video_extensions or '.strm' in self.metadata_extensions:raise ValueError('不能把 STRM 再作为源文件生成或下载')
        if set(self.video_extensions)&set(self.metadata_extensions):raise ValueError('生成视频和实体下载扩展名不能重复')
        return self


def effective(store, sync_value=None, organize_value=None):
    organize=options(store.get('organize_config',{}) if organize_value is None else organize_value)
    raw=StrmOptions.model_validate(store.get('sync_config',{}) if sync_value is None else sync_value).model_dump()
    if raw['inherit_video_extensions']:raw['video_extensions']=organize['video_extensions']
    if set(raw['video_extensions'])&set(raw['metadata_extensions']):raise ValueError('整理视频扩展名与 STRM 实体下载扩展名重复，请修改后保存')
    return raw


def skip_reason(source,config):
    if Path(source['name']).suffix.lower() not in set(config['video_extensions']):return '未列入 STRM 生成扩展名'
    if config['min_video_size_mb'] and float(source.get('size') or 0)<config['min_video_size_mb']*1024*1024:return f"小于 STRM 生成阈值 {config['min_video_size_mb']:g} MB"
    return ''


def shared_settings(store):
    organize=options(store.get('organize_config',{}));strm=effective(store)
    quality=public_policy(store.get('upgrade_policy'))
    value={'organize':organize,'strm':strm,'upgrade_policy':quality,'playback_base':store.get('playback_base') or '',
           'notify_emby':store.get('sync_config',{}).get('notify_emby',True)}
    value['version']=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
    return value


def share_effective_settings(store):
    """Existing installations follow shared settings until a share profile is saved."""
    value=shared_settings(store)
    profile=store.get('share_profile') or {}
    if profile.get('organize') is not None and profile.get('upgrade_policy') is not None:
        value['organize']=options(profile['organize'])
        value['upgrade_policy']=public_policy(profile['upgrade_policy'])
        value['strm']=effective(store,organize_value=value['organize'])
        value['notify_emby']=bool(profile.get('notify_emby',value['notify_emby']))
    value['version']=hashlib.sha256(json.dumps({k:v for k,v in value.items() if k!='version'},sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
    return value


def write_new(path,data,root):
    """Incremental metadata download: an existing file is never replaced.

    统一走 fsutil.write_once：硬链接不可用的挂载（CIFS/NFS 的 EPERM/EXDEV）
    退回 O_EXCL 直写，不再让整批附属文件失败。
    """
    from .fsutil import UnsafePath, write_once
    root=Path(root).resolve();path=Path(path)
    if any(p.is_symlink() for p in path.parents if p!=root and root in p.parents):raise ValueError('附属文件路径不安全')
    try:
        return write_once(path,data,root)
    except UnsafePath as exc:
        raise ValueError('附属文件路径不安全') from exc


async def copy_cloud_metadata(engine,jid,entries,destination):
    import httpx
    from .cloud115 import CloudError
    from .media import component
    for source in entries:
        engine.boundary(jid)
        path=Path(destination)/component(source['name'])
        if path.exists():continue
        if (source.get('size') or 0)>20*1024*1024:
            engine.store.event('strm','附属文件超过20MB，已跳过：'+source['name'])
            continue
        try:
            current=await engine.cloud.file(source['id'])
            engine.check_source(source,current,source['name'],source['parent_id'])
            url,headers=await engine.cloud.url(current['pickcode'],'StrimKeep/metadata')
            data=await engine.download(url,{**headers,'User-Agent':'StrimKeep/metadata'},20*1024*1024)
            engine.check_source(source,await engine.cloud.file(source['id']),source['name'],source['parent_id'])
            write_new(path,data,engine.root)
        except (ValueError,httpx.HTTPError,CloudError) as exc:
            if isinstance(exc,CloudError) and exc.code in ('LOGIN_REQUIRED','LOGIN_INVALID'):raise
            engine.store.event('strm','附属文件下载未完成，保留已有文件：'+source['name'])
