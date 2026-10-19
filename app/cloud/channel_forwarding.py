"""User-session command relay. Writes have durable claims and no implicit retry."""
import asyncio
from app.cloud import channel_reader, credentials, services, jobs
from app.cloud.errors import CloudError
from app.domain.telegram_intake import relay_text


async def _send(secret, target, text, guard):
    client = channel_reader._client(secret)
    dispatched = False
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise CloudError('Telegram 用户会话已失效，请重新登录', 409, stage='forward')
        entity = await client.get_entity(target)
        if not getattr(entity, 'bot', False):
            raise CloudError('转发目标必须是 Telegram 机器人，请核对 @用户名', 409, stage='forward')
        guard()
        dispatched = True
        sent = await client.send_message(entity, text, parse_mode=None, link_preview=False)
        if not isinstance(getattr(sent, 'id', None), int) or sent.id <= 0:
            raise CloudError('转发消息回执缺失，请在 Telegram 核查，未自动重发', 502, uncertain=True, stage='forward')
        return {'kind': 'forward', 'target': target, 'message_id': sent.id, 'completed': True,
                'name': '命令消息已发送（不代表目标机器人已处理）'}
    except CloudError:
        raise
    except Exception as exc:
        # Explicit RPC refusal proves no accepted send; timeout/disconnect does not.
        from telethon.errors import RPCError
        definite = isinstance(exc, RPCError) and getattr(exc, 'code', 500) < 500
        raise CloudError('Telegram 拒绝转发，请检查目标、权限或限流' if definite else
                         '转发结果待核实，请在 Telegram 查看消息，未自动重发' if dispatched else
                         '连接转发机器人失败，请检查用户会话和网络',
                         502, uncertain=dispatched and not definite, stage='forward') from None
    finally:
        await client.disconnect()


def run(job_id, payload, rule, message, guard):
    try:
        text = relay_text(rule['command_prefix'], message)
    except ValueError as exc:
        raise CloudError(str(exc), stage='forward') from None
    cfg = services.get(payload['account_id'], 'telegram_reader', channel_reader.DEFAULT)
    if cfg['account_revision'] != payload['account_revision'] or not cfg['config']['sealed']:
        raise CloudError('万能转发到其他机器人需要先登录 Telegram 用户会话', 409, stage='forward')
    def current():
        jobs.checkpoint(job_id)
        guard()
        now = services.get(payload['account_id'], 'telegram_reader', channel_reader.DEFAULT)
        if now['revision'] != cfg['revision'] or now['account_revision'] != cfg['account_revision']:
            raise CloudError('Telegram 会话在转发前变化，已停止', 409, stage='forward')
    def send():
        current()
        return asyncio.run(asyncio.wait_for(_send(credentials.unseal(cfg['config']['sealed']), rule['forward_target'], text, current), 40))
    # All text/URLs remain encrypted in the inbox. Steps retain only routing evidence.
    def protected_send():
        try:
            return send()
        except (TimeoutError, OSError):
            raise CloudError('转发结果待核实，请在 Telegram 查看消息，未自动重发', 502, uncertain=True, stage='forward') from None
    return jobs.step(job_id, 'telegram-forward', {'target': rule['forward_target']}, protected_send)
