"""Safe provider diagnostics. Never echo provider messages, URLs or credentials."""
import re

OPERATIONS = {'user_my': ('account', '核验账号'), 'fs_files': ('list', '读取目录'),
              'fs_file': ('info', '读取文件信息'), 'fs_file_skim': ('info', '读取文件信息'),
              'fs_mkdir': ('mkdir', '创建目录'), 'fs_rename': ('rename', '重命名'),
              'fs_move': ('move', '移动归档'), 'fs_delete': ('delete', '删除文件')}


def stops_batch(category):
    return category in ('authorization', 'verification_required', 'rate_limit', 'transport', 'response')


def operation(method):
    if method == 'share_receive':
        return 'submit_transfer', '提交转存'
    if method.startswith('share_'):
        return 'read_share', '读取分享'
    if method.startswith('download'):
        return 'download', '获取下载地址'
    return OPERATIONS.get(method, ('provider', '调用 115 接口'))


def transport(method, type_names, status=None, write=False):
    """Classify only exception TYPE names and HTTP status, never exception text."""
    stage, label = operation(method)
    names = set(type_names)
    name = next((n for n in type_names if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', n)), 'Exception')
    code, category, reason = 'provider_exception', 'response', '接口调用异常（'+name+'），请按错误类型检查任务日志'
    if status is not None:
        code, category, reason = 'provider_http_'+str(status), 'transport', 'HTTP '+str(status)
        if status in (401, 403):
            category, reason = 'authorization', reason+'，账号授权失效或访问被拒绝，请检查 115 登录与网络'
        elif status == 429:
            category, reason = 'rate_limit', reason+'，请求过于频繁，请等待后重试'
        elif status >= 500:
            reason += '，115 服务或代理暂时不可用'
        elif status == 404:
            category, reason = 'not_found', reason+'，接口或文件不存在'
    elif any('Timeout' in name for name in names):
        code, category, reason = 'provider_timeout', 'transport', '请求超时，请检查容器到 115 的连接'
    elif 'gaierror' in names:
        code, category, reason = 'provider_dns', 'transport', 'DNS 解析失败，请检查容器 DNS'
    elif any('SSL' in name or 'Certificate' in name for name in names):
        code, category, reason = 'provider_tls', 'transport', 'TLS 证书校验失败，请检查系统时间与代理证书'
    elif any('JSONDecode' in name or 'Decoding' in name for name in names):
        code, category, reason = 'provider_response', 'response', '响应格式异常，可能收到登录页、限流页或代理错误页'
    elif any('Proxy' in name for name in names):
        code, category, reason = 'provider_proxy', 'transport', '代理连接失败，请检查服务连接与容器代理'
    elif names.intersection(('NetworkError', 'ConnectionError', 'ConnectError', 'OSError', 'ProtocolError', 'RemoteProtocolError')):
        code, category, reason = 'provider_transport', 'transport', '网络连接中断（'+name+'），请检查容器到 115 的连接'
    message = '115 '+label+'失败：'+reason
    if write:
        message += '；写入结果待核实，请先检查任务步骤和网盘实际状态，不要重复提交'
    return {'stage': stage, 'code': code, 'category': category, 'message': message}


def classify(response, method=''):
    code = next((str(response[k]) for k in ('errno', 'errNo', 'errcode', 'errCode', 'code', 'msg_code')
                 if k in response and re.fullmatch(r'-?[0-9]{1,12}', str(response[k]))), '')
    hint = ' '.join(str(response.get(k, ''))[:300] for k in ('error', 'message', 'msg', 'error_msg')).casefold()
    share = method.startswith('share_')
    category, message = 'provider_rejected', '115 拒绝请求，请检查账号状态与操作条件'
    if code in ('99', '990001', '40101032', '40101017'):
        category, message = 'authorization', '115 账号授权异常，请检查 Cookie 登录状态并重新授权'
    elif code == '911':
        category, message = 'verification_required', '115 账号需要安全验证，请在官方客户端完成验证'
    elif share and (code == '4100010' or re.search(r'分享.{0,12}(?:取消|撤销)|share.{0,12}cancel', hint)):
        category, message = 'share_cancelled', '115 分享已取消，请更换有效分享链接'
    elif share and re.search(r'(?:分享|链接).{0,12}(?:过期|到期)|(?:expired|expiration)', hint):
        category, message = 'share_expired', '115 分享已过期，请重新获取分享链接'
    elif share and re.search(r'(?:提取码|访问码|密码|receive.?code|password).{0,16}(?:错误|不正确|不匹配|无效|incorrect|invalid|wrong)', hint):
        category, message = 'share_password', '115 分享提取码错误，请检查访问密码后重试'
    elif share and code == '4100026':
        category, message = 'share_unavailable', '115 分享不存在或已删除，请更换有效分享链接'
    elif re.search(r'请重新登[录陆]|登录.{0,8}(?:失效|超时)|未登录', hint):
        category, message = 'authorization', '115 账号授权异常，请检查 Cookie 登录状态并重新授权'
    return {'code': code, 'category': category, 'message': message + ('（错误码 ' + code + '）' if code else '')}
