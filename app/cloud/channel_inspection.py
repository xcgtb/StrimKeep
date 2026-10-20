"""Read-only channel inspection. Never captures inbox records or advances cursors."""
import asyncio

from app.cloud import receiving, channel_reader, credentials, services, store, channel_network
from app.cloud.errors import CloudError
from app.domain import telegram_intake


def evaluate(rule, message):
    normalized = telegram_intake.content(message)
    text = normalized['text']
    filter_text = text + '\n' + '\n'.join(e['url'] for e in normalized['entities'] if 'themoviedb.org/' in e['url'])
    matched, reason = receiving.allowed_message(filter_text, rule)
    destination, label = telegram_intake.delivery_route(rule)
    result = {'matched': matched, 'filter_matched': matched, 'reason': reason,
              'action': rule.get('action', 'transfer'), 'destination': label,
              'target': rule.get('forward_target', 'self'), 'resource_count': 0,
              'preview_only': True, 'note': '仅检查，不保存消息、不推进读取位置、不提交转存或发送命令'}
    # An external relay may intentionally carry arbitrary text or non-115 URLs.
    if destination != 'external':
        try:
            items = receiving.extract(text, normalized['entities'])
            result['resource_count'] = len(items)
            eligible, resource_reason = telegram_intake.delivery_eligibility(rule, [i['kind'] for i in items])
            if matched and not eligible:
                result.update(matched=False, reason=resource_reason)
        except CloudError as exc:
            result.update(matched=False, reason=str(exc))
    if rule.get('action') == 'forward':
        try:
            result['preview_text'] = telegram_intake.relay_text(rule.get('command_prefix', '/strm'), normalized)
        except ValueError as exc:
            result.update(matched=False, reason=str(exc))
    if not result['matched']:
        result['would_do'] = '跳过：' + result['reason']
    elif not rule.get('enabled'):
        result['would_do'] = '规则已暂停；启用后才会处理新消息'
    elif not rule.get('auto_transfer', True):
        result['would_do'] = '仅记录匹配消息，不执行处理'
    else:
        result['would_do'] = label
    return result


@channel_network.probe
def inspect(account_id, channel_id):
    rule = next((row for row in receiving.channels(account_id) if row['id'] == channel_id), None)
    if rule is None:
        raise CloudError('频道规则不存在', 404)
    account = store.account(account_id)
    if not account:
        raise CloudError('账号不存在', 404)
    reader = services.get(account_id, 'telegram_reader', channel_reader.DEFAULT)
    destination, label = telegram_intake.delivery_route(rule)
    checks = []

    def check(label, ok, detail):
        checks.append({'label': label, 'ok': ok, 'detail': detail})

    check('规则开关', bool(rule['enabled']), '已启用' if rule['enabled'] else '已暂停；检查不会自动启用')
    check('处理方式', bool(rule.get('auto_transfer', True)), '匹配后自动处理' if rule.get('auto_transfer', True) else '观察模式：仅记录匹配消息')
    if rule['mode'] == 'user' or destination == 'external':
        check('Telegram 用户会话', bool(reader['config']['sealed']), '已保存会话；本次读取将验证连接' if rule['mode'] == 'user' and reader['config']['sealed'] else '已保存会话；实际发送时验证权限' if reader['config']['sealed'] else '尚未登录，请到用户会话完成登录')
    if destination != 'external':
        check('115 执行', None, '本次不访问 115；Cookie、分享有效性和输出配置在实际任务中核验')
    messages = []
    read_error = ''
    try:
        if rule['mode'] == 'public_web':
            from app.cloud import public_channel_reader
            from app.cloud.channel_network import describe
            check('网络路径', None, describe()['label'])
            messages, _ = public_channel_reader.fetch_messages(rule['address'].lstrip('@'), history_limit=5)
            check('读取频道', True, f'从公开预览读取最近 {len(messages)} 条消息；网页可能不包含全部内容')
        elif rule['mode'] == 'user':
            if not reader['config']['sealed']:
                raise CloudError('请先登录 Telegram 用户会话', 409)
            secret = credentials.unseal(reader['config']['sealed'])
            messages, _, _ = channel_reader._run(channel_reader._messages(secret, {**rule, 'history_limit': 5}, 0, False))
            check('读取频道', True, f'通过用户会话读取最近 {len(messages)} 条消息')
        else:
            check('读取频道', None, 'Bot 模式只能接收后续实时更新，无法主动读取历史；请在频道发一条新消息后查看接收历史')
    except CloudError as exc:
        read_error = str(exc)
        check('读取频道', False, read_error)
    except (asyncio.TimeoutError, OSError):
        read_error = '频道读取超时或网络不可达，请检查容器网络后重试'
        check('读取频道', False, read_error)
    samples = []
    for message in messages[:5]:
        try:
            evaluation = evaluate(rule, message)
        except CloudError as exc:
            evaluation = {'matched': False, 'reason': str(exc), 'would_do': '筛选查询失败，未处理'}
        samples.append({'message_id': message['message_id'], 'text': message.get('text', '')[:1200], **evaluation})
    # Do not present results collected with stale account/rule/session identities.
    current = next((row for row in receiving.channels(account_id) if row['id'] == channel_id), None)
    current_account = store.account(account_id)
    current_reader = services.get(account_id, 'telegram_reader', channel_reader.DEFAULT)
    if (not current or not current_account or current['revision'] != rule['revision'] or
            current_account['revision'] != account['revision'] or
            ((rule['mode'] == 'user' or destination == 'external') and current_reader['revision'] != reader['revision'])):
        raise CloudError('检查期间账号、规则或会话已变化，请重新检查', 409)
    return {'preview_only': True, 'channel': rule['name'], 'action_label': label,
            'checks': checks, 'messages': samples, 'read_error': read_error,
            'note': '本次检查未保存消息、未改变读取位置，也没有触发转存、STRM 生成或外部转发'}
