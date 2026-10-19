"""Pure Telegram message normalization and bounded command relay rendering."""
import re

SHARE_COMMANDS = {'/strm', '/sharestrm', '/create_115share_strm'}
BUILTIN_COMMANDS = SHARE_COMMANDS | {'/receive'}


def command(text):
    return (str(text).strip().split() or [''])[0].split('@')[0].lower()


def content(msg, allow_reply=False):
    text = str(msg.get('text') or msg.get('caption') or '')[:16000]
    entities = msg.get('entities') or msg.get('caption_entities') or []
    entities = [{'type': 'text_link', 'url': e['url'][:4096]} for e in entities[:200]
                if isinstance(e, dict) and e.get('type') == 'text_link' and isinstance(e.get('url'), str)]
    keyboard = (msg.get('reply_markup') or {}).get('inline_keyboard') or []
    for row in keyboard[:20]:
        for button in row[:20]:
            if isinstance(button, dict) and isinstance(button.get('url'), str):
                entities.append({'type':'text_link','url':button['url'][:4096]})
    # A command replying to a forwarded photo/post consumes that immediate reply only.
    # Authorization always comes from the caller's sender, never from forwarded metadata.
    reply = msg.get('reply_to_message')
    if allow_reply and command(text) in BUILTIN_COMMANDS and isinstance(reply, dict):
        body = content(reply)
        text = (text + '\n' + body['text'])[:16000]
        entities += body['entities']
    return {'text': text, 'entities': entities[:200], 'share_direct': command(text) in SHARE_COMMANDS}


def validate_relay(prefix, target):
    if not isinstance(prefix, str) or len(prefix) > 160 or not re.fullmatch(r'/[A-Za-z][A-Za-z0-9_]{0,63}(?:@\w{5,32})?(?: [^\r\n\x00-\x1f]*)?', prefix):
        raise ValueError('命令头需以 /命令 开始，可附加一行参数，最长160字')
    if not isinstance(target, str) or not (target == 'self' or re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{4,31}', target)):
        raise ValueError('转发目标请选择内置接收或填写 @机器人用户名')
    if target == 'self' and command(prefix) not in BUILTIN_COMMANDS:
        raise ValueError('内置接收支持 /strm、/sharestrm、/create_115share_strm 或 /receive')


def relay_text(prefix, message):
    body = message['text'].strip()
    # Replacing the first command prevents nested /strm /receive command headers.
    if command(body).startswith('/'):
        body = re.sub(r'^/\S+\s*', '', body, count=1)
    hidden = [e['url'] for e in message.get('entities', []) if e['url'] not in body]
    value = prefix + '\n' + body + ('\n' + '\n'.join(dict.fromkeys(hidden)) if hidden else '')
    if len(value.encode('utf-16-le')) // 2 > 4096:
        raise ValueError('转发内容超过 Telegram 4096 字符限制，请缩小来源消息；未发送截断内容')
    return value



def delivery_route(rule):
    action = rule.get('action', 'transfer')
    if action == 'forward' and rule.get('forward_target', 'self') != 'self':
        return 'external', '发送给 ' + rule['forward_target']
    if action == 'share_strm' or (action == 'forward' and command(rule.get('command_prefix', '/strm')) in SHARE_COMMANDS):
        return 'share_strm', '内置生成分享 STRM（免转存）'
    return 'transfer', '转存到 115 接收目录'


def delivery_eligibility(rule, kinds):
    destination, _ = delivery_route(rule)
    if destination == 'external':
        return True, '可发送命令消息；对方处理结果需在目标机器人查看'
    if not kinds:
        return False, '没有找到支持的资源链接'
    if rule.get('mode') == 'public_web':
        kinds = [kind for kind in kinds if kind == 'share']
        if not kinds:
            return False, '公开网页自动处理仅支持 115 分享链接'
    if destination == 'share_strm' and any(kind != 'share' for kind in kinds):
        return False, '分享 STRM 只接受 115 分享链接'
    return True, '资源类型符合当前处理动作'
