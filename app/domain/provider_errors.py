"""Safe provider diagnostics. Never echo provider messages, URLs or credentials."""
import re


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
