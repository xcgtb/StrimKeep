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
           'channel':'cloud_channel','maintenance':'cloud_maintenance'}


def clean(value):
    text = str(value)
    text = re.sub(r'https?://[^\s<>]+', '[链接已隐藏]', text, flags=re.I)
    text = re.sub(r'(?i)\b(cookie|authorization|uid|cid|seid|kid|token|password|api[_-]?key|api_hash|session|phone_code_hash)\s*[:=]\s*[^\s;,]+', r'\1=[hidden]', text)
    text = re.sub(r'(提取码|访问码|密码)\s*[:：=]?\s*[a-zA-Z0-9]+', r'\1：[hidden]', text)
    return runtime_logs.redact(text)[:2000]


def emit(stage, message, job_id=None, level='INFO', con=None):
    if stage not in SOURCES or level not in ('INFO','WARNING','ERROR'):
        raise ValueError('invalid cloud event')
    identifier = job_id or _JOB.get()
    prefix = '[任务 '+str(identifier)[:12]+'] ' if identifier else ''
    row = {'ts':datetime.now().isoformat(timespec='seconds'),'level':level,
           'source':SOURCES[stage],'message':prefix+clean(message)}
    def insert(db):
        rid = runtime_logs._insert(db,row)
        if rid % 100 == 0:
            db.execute('DELETE FROM runtime_logs WHERE id<=?',(rid-runtime_logs.MAX_LOGS,))
    if con is not None:
        insert(con)
    else:
        with store.db() as db:
            insert(db)
