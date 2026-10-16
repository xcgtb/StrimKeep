"""Cloud lifecycle events in the existing live log stream, including caller transactions."""
import contextvars
import re
from datetime import datetime
from app import runtime_logs
from app.cloud import store

_JOB = contextvars.ContextVar('cloud_log_job', default='')
SOURCES = {'queue':'cloud_queue','transfer':'cloud_transfer','organize':'cloud_organize',
           'recognize':'cloud_recognize','probe':'cloud_probe','naming':'cloud_naming',
           'strm':'cloud_strm','emby':'cloud_emby','notify':'cloud_notify',
           'channel':'cloud_channel','maintenance':'cloud_maintenance','governance':'cloud_governance'}

DETAIL_FIELDS = frozenset(('job_id','event','status','file_id','parent_id','target_id',
    'source_name','target_name','path','sha1','size','asset_id','tmdb_id','kind',
    'season','episode','elapsed_ms','cache_hit','source_count','result','connection',
    'count','reason','account_id','binding_version','directory_count','file_count',
    'video_count','filtered_count','skipped_count','published_count','failed_count',
    'metadata_count','queued_count','renamed_count','moved_count','removed_count'))


def safe_details(value):
    """Only explicit diagnostic fields; never serialize provider replies/credentials."""
    return {k: (v if type(v) in (bool,int) else clean(v)[:1000])
            for k,v in (value or {}).items() if k in DETAIL_FIELDS and type(v) in (str,bool,int)}


def clean(value):
    text = str(value)
    text = re.sub(r'https?://[^\s<>]+', '[链接已隐藏]', text, flags=re.I)
    text = re.sub(r'(?i)\b(cookie|authorization|uid|cid|seid|kid|token|password|api[_-]?key|api_hash|session|phone_code_hash)\s*[:=]\s*[^\s;,]+', r'\1=[hidden]', text)
    text = re.sub(r'(提取码|访问码|密码)\s*[:：=]?\s*[a-zA-Z0-9]+', r'\1：[hidden]', text)
    return runtime_logs.redact(text)[:2000]


def emit(stage, message, job_id=None, level='INFO', con=None, details=None):
    if stage not in SOURCES or level not in ('INFO','WARNING','ERROR'):
        raise ValueError('invalid cloud event')
    identifier = job_id or _JOB.get()
    prefix = '[任务 '+str(identifier)[:12]+'] ' if identifier else ''
    row = {'ts':datetime.now().isoformat(timespec='milliseconds'),'level':level,
           'source':SOURCES[stage],'message':prefix+clean(message),
           'details':safe_details({**(details or {}),**({'job_id':identifier} if identifier else {})})}
    def insert(db):
        rid = runtime_logs._insert(db,row)
        if rid % 100 == 0:
            db.execute('DELETE FROM runtime_logs WHERE id<=?',(rid-runtime_logs.MAX_LOGS,))
    if con is not None:
        insert(con)
    else:
        with store.db() as db:
            insert(db)
