"""Explicit auxiliary Cookie scope for 115 share receive, never cloud delete."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from .p115_open import ProviderError,identifier,_NETWORK,_NoRedirect,MAX_RESPONSE

ROUTES={'user':('GET','https://webapi.115.com/user/info'),
        'share':('GET','https://webapi.115.com/share/snap'),
        'receive':('POST','https://webapi.115.com/share/receive')}


def cookie(value):
    if not isinstance(value,str) or not 1<=len(value)<=8192 or any(ord(c)<32 or ord(c)>126 for c in value):
        raise ProviderError('请填写完整的 115 Web Cookie，不包含换行',400)
    fields={}
    for pair in value.strip().split(';'):
        pair=pair.strip()
        if not pair:continue
        key,sep,item=pair.partition('=')
        if not sep or key not in ('UID','CID','SEID','KID') or key in fields or not re.fullmatch(r'[A-Za-z0-9._=+/-]{1,4096}',item):
            raise ProviderError('辅助 Cookie 仅接受 UID/CID/SEID/KID 字段',400)
        fields[key]=item
    if not all(k in fields for k in ('UID','CID','SEID')):raise ProviderError('辅助 Cookie 缺少 UID/CID/SEID',400)
    uid=identifier(fields['UID'].split('_',1)[0])
    return '; '.join(k+'='+fields[k] for k in ('UID','CID','SEID','KID') if k in fields),uid


def transport(action,payload=None,web_cookie=''):
    method,url=ROUTES[action];payload=payload or {}
    encoded=urllib.parse.urlencode(payload).encode()
    if method=='GET' and payload:url+='?'+encoded.decode()
    headers={'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded',
             'User-Agent':'Mozilla/5.0 StrimKeep/115Share','Referer':'https://webapi.115.com/'}
    if web_cookie:headers['Cookie']=web_cookie
    if not _NETWORK.acquire(False):raise ProviderError('115 请求繁忙，请稍后重试',429)
    try:
        request=urllib.request.Request(url,data=encoded if method=='POST' else None,method=method,headers=headers)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
        try:
            with opener.open(request,timeout=20) as response:
                raw=response.read(MAX_RESPONSE+1)
                if len(raw)>MAX_RESPONSE or 'json' not in response.headers.get('Content-Type','').lower():
                    raise ProviderError('115 分享响应不是可确认的 JSON')
                result=json.loads(raw)
        except urllib.error.HTTPError as error:
            error.close()
            raise ProviderError('115 分享 HTTP 请求未确认；不会自动重发转存') from None
        except (urllib.error.URLError,TimeoutError,OSError,ValueError):
            raise ProviderError('115 分享请求结果未确认；不会自动重发转存') from None
        if not isinstance(result,dict) or not (result.get('state') is True or type(result.get('state')) is int and result['state']==1):
            raise ProviderError('115 分享接口未确认成功，请核对辅助授权和分享权限')
        return result
    finally:_NETWORK.release()


def user_id(result):
    data=result.get('data')
    if not isinstance(data,dict):raise ProviderError('115 辅助授权身份暂不兼容')
    uid=data.get('user_id',data.get('uid'))
    if data.get('user_id') is not None and data.get('uid') is not None and str(data['user_id'])!=str(data['uid']):
        raise ProviderError('115 辅助授权返回账号身份冲突',409)
    return identifier(uid)


def share_link(value):
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<32 for c in value):raise ProviderError('分享地址无效',400)
    try:
        parts=urllib.parse.urlsplit(value.strip())
        match=re.fullmatch(r'/s/([A-Za-z0-9]{6,64})/?',parts.path)
        params=urllib.parse.parse_qs(parts.query,keep_blank_values=True)
        passwords=params.get('password',[])+params.get('receive_code',[])
        if parts.scheme not in ('http','https') or parts.hostname!='115.com' or parts.username or parts.password or parts.fragment or not match or parts.port not in (None,80,443):raise ValueError()
        if len(passwords)>1 or passwords and not re.fullmatch(r'[A-Za-z0-9]{4}',passwords[0]):raise ValueError()
        return {'share_code':match[1],'receive_code':passwords[0] if passwords else ''}
    except ValueError:raise ProviderError('仅支持 115.com/s/ 分享地址及唯一四位访问码',400) from None


def normalize_share(row):
    if not isinstance(row,dict):raise ProviderError('115 分享文件结构暂不兼容')
    is_dir=row.get('fid') in (None,'',0,'0')
    fid=identifier(row.get('cid') if is_dir else row.get('fid'))
    name=row.get('n',row.get('fn',row.get('file_name')))
    if not isinstance(name,str) or not name or len(name)>1024:raise ProviderError('分享文件名缺失')
    size=row.get('s',row.get('fs'))
    if not is_dir and (isinstance(size,bool) or not re.fullmatch(r'\d+',str(size))):raise ProviderError('分享文件大小缺失')
    sha=str(row.get('sha',row.get('sha1','')) or '').upper()
    if sha and not re.fullmatch(r'[A-F0-9]{40}',sha):raise ProviderError('分享校验信息无效')
    return {'id':fid,'name':name,'is_dir':is_dir,'size':int(size) if not is_dir else 0,'sha1':sha}


def page(result,offset,limit):
    data=result.get('data')
    rows=data.get('list') if isinstance(data,dict) else None
    if not isinstance(rows,list) or len(rows)>limit:raise ProviderError('分享目录响应暂不兼容')
    items=[normalize_share(row) for row in rows]
    if len({x['id'] for x in items})!=len(items):raise ProviderError('分享目录包含重复文件 ID')
    return {'items':items,'next_offset':offset+len(items) if len(items)==limit else None}
