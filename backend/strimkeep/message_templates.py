"""Plain-text Telegram messages shared by channel, bot and transfer jobs."""
import re


def compact(value, limit=100):
    text = ' '.join(str(value or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def sync_task_context(config):
    """Keep sync notifications attributable without guessing legacy task modes."""
    lines=[]
    name=str(config.get('sync_profile_name') or '').strip()
    if name: lines.append('任务：'+name)
    # 任务配置里写的是 sync_mode（mode 是老字段），两个都认，免得通知里看不到方式。
    mode={'incremental':'增量同步','full':'全量同步','sweep':'本地巡检'}.get(
        config.get('mode') or config.get('sync_mode'))
    if mode: lines.append('方式：'+mode)
    return '\n'.join(lines)+'\n' if lines else ''


def channel_post_text(text):
    """Keep the post heading and readable notes out of the resource links."""
    lines = []
    for raw in str(text or '').splitlines():
        line = re.sub(r'(?i)(?:https?://|ftp://|tg://|magnet:\?|ed2k://)\S+', '', raw)
        line = re.sub(r'(?:提取码|访问码|密码)\s*[:：=]?\s*[A-Za-z0-9]+', '', line)
        line = re.sub(r'(?i)\b(?:password|pwd)\s*[:：=]\s*[A-Za-z0-9]+', '', line)
        line = line.strip(' \t-–—:：|｜•·')
        if line and re.search(r'[\w\u4e00-\u9fff]', line):
            lines.append(line)
    if not lines:
        return '', ''
    if lines[0].startswith('#'):
        return '', compact(' · '.join(lines), 230)
    return compact(lines[0], 100), compact(' · '.join(lines[1:]), 230)


def channel_subject(text):
    return channel_post_text(text)[0]


def channel_post_lines(context):
    lines = []
    if context.get('subject'): lines.append('🎬 ' + context['subject'].removeprefix('🎬').strip())
    if context.get('channel'): lines.append('📡 来源  ' + compact(context['channel'], 90))
    if context.get('note'): lines.append('📝 帖子备注  ' + context['note'])
    if context.get('post_url'): lines.append('↗ 频道原帖  ' + context['post_url'])
    return lines


def task_numbers(ids):
    numbers = [f'#{int(value)}' for value in ids if isinstance(value, int)]
    return '、'.join(numbers[:10]) + (f' 等 {len(numbers)} 个' if len(numbers) > 10 else '')


def transfer_received(ids, target, auto):
    lines = ['已接收分享，正在排队处理。', '',
             f'📁 保存至  {compact(target) or "已配置的转存目录"}',
             f'🔗 分享数  {len(ids)} 个']
    if task_numbers(ids): lines.append('📋 任务  ' + task_numbers(ids))
    return '\n'.join(lines)


def channel_notice(channel, text, queued, mode, target='', count=None, resource_kind='', omitted=0,
                   *, context=None, submission_id=None):
    heading = {'transfer':'📡 频道分享转存 · 已排队', 'offline':'📡 频道离线下载 · 已提交',
               'strm':'📡 频道分享 STRM · 已生成', 'offline_unknown':'⚠️ 频道离线下载 · 待核查'}.get(mode, '📡 频道资源')
    if context is None:
        subject, note = channel_post_text(text)
        context = {'channel': channel, 'subject': subject, 'note': note}
    lines = channel_post_lines(context)
    if mode == 'strm':
        lines.extend(['🔗 模式  分享 STRM · 免转存', '📂 位置  分享 STRM 待整理',
                      f'📄 已生成  {count if count is not None else 0} 个 STRM'])
        ids = task_numbers([item[0] for item in queued])
        if ids: lines.append('📋 整理任务  ' + ids)
    elif mode == 'offline_unknown':
        if resource_kind: lines.append('🧲 类型  ' + resource_kind)
        if target: lines.append('📂 目标  ' + compact(target))
        lines.extend(['⚠️ 提交结果未确认', '请先检查 115 离线任务，确认后再操作，避免重复提交。'])
    elif mode == 'offline':
        if resource_kind: lines.append('🧲 类型  ' + resource_kind)
        if target: lines.append('📂 保存至  ' + compact(target))
        if submission_id is not None: lines.append(f'📋 离线记录  #{submission_id}')
        if count is not None: lines.append(f'📨 已提交  {count} 个链接')
        if omitted: lines.append(f'⚠️ 超过单次上限，{omitted} 个链接未提交')
        lines.append('⏳ 状态  已提交 115，下载结果待确认')
    else:
        if target: lines.append('📂 保存至  ' + compact(target))
        lines.append(f'🔗 分享数  {len(queued)} 个')
        ids = task_numbers([item[0] for item in queued])
        if ids: lines.append('📋 转存任务  ' + ids)
    return heading, '\n'.join(lines)


def offline_completed(target, produced, job_id=None, *, context=None, submission_id=None,
                      task_names=(), actual_location=True):
    context = context or {}
    heading = ('✅ 频道离线下载' if context.get('channel') else '✅ 离线下载') + ' · 已完成'
    lines = offline_context_lines(context, submission_id)
    location = '落盘' if produced and actual_location else '提交目录'
    lines.append(f'📂 {location}  {compact(target) or "已配置目录"}')
    if not produced:
        lines.append('⚠️ 115 已完成，暂未核实落盘文件')
        if task_names:
            lines.append('📄 115 任务  ' + compact('、'.join(task_names), 180))
        return heading, '\n'.join(lines)
    lines.append(f'📄 已核实落盘  {len(produced)} 项')
    for entry in produced[:8]:
        name = compact(entry.get('name'), 135) or '文件名未返回'
        lines.append(('📁 ' if entry.get('is_dir') else '· ') + name)
    if len(produced) > 8:
        lines.append(f'另有 {len(produced) - 8} 项已落盘')
    lines.append(f'🎬 整理  任务 #{job_id} 已入队' if job_id else '📥 处理  仅落盘')
    return heading, '\n'.join(lines)


def offline_context_lines(context, submission_id):
    lines = channel_post_lines(context)
    if context.get('resource_kind'): lines.append('🧲 类型  ' + context['resource_kind'])
    if submission_id is not None: lines.append(f'📋 离线记录  #{submission_id}')
    return lines


def offline_stalled(target, count, name='', *, context=None, submission_id=None):
    """离线下载超时回执：一条提交一条，用户拿着文件名就能去 115 换源。"""
    context = context or {}
    heading = ('⏳ 频道离线下载' if context.get('channel') else '⏳ 离线下载') + ' · 超时'
    lines = offline_context_lines(context, submission_id)
    lines.append(f'📂 目标  {compact(target) or "已配置目录"}')
    lines.append(f'📄 文件  {compact(name, 70)}' if name else f'🔗 链接  {count} 个')
    lines.append('👉 到 115 离线列表删掉换源')
    return heading, '\n'.join(lines)


def transfer_completed(job_id, config, message):
    receipt = config.get('receipt') or {}
    origin = {'channel':'频道监控', 'telegram':'机器人', 'web':'手动转存'}.get(config.get('origin'), '手动转存')
    lines = [f'📡 来源  {origin}', '📂 保存至  ' + compact(config.get('target_label') or '目标目录')]
    items = receipt.get('received_entries')
    if isinstance(items, list): lines.append(f'📦 已保存  {len(items)} 个文件或文件夹')
    lines.append(f'📋 任务  #{job_id}')
    if receipt.get('organize_job'):
        lines.append('📋 整理任务  #%s' % receipt['organize_job'])
    elif config.get('origin') == 'channel':
        # The existing message carries actionable scan-configuration warnings.
        if '目标目录未被' in message or '定时扫描未启用' in message:
            lines.extend(['', '⚠️ ' + message.split('。', 1)[-1].strip()])
    return '📥 转存完成', '\n'.join(lines)


def cloud_sign_notice(signed, days=None, date='', reward=None, balance=None):
    # 115 签到回执：emoji + 两空格对齐；结论写在标题里，不再追一行「后续」。
    heading = '☑️ 115 签到 · 已完成' if signed else '☑️ 115 签到 · 今天已签'
    lines = []
    if date:
        lines.append('📅 日期  ' + date)
    if days:
        lines.append('🔥 连续  %s 天' % days)
    if reward is not None:
        lines.append('🍁 今日获得  %s 枫叶' % reward)
    if balance is not None:
        lines.append('💰 当前余额  %s 枫叶' % balance)
    if days is None or reward is None or balance is None:
        lines.append('ℹ️ 部分签到详情暂未返回')
    return heading, '\n'.join(lines)


def cloud_recycle_notice(count=None, when=''):
    # 115 回收站清空回执。
    lines = []
    if count is not None:
        lines.append('🗑 清理  %s 个文件' % count)
    if when:
        lines.append('🕒 时间  ' + when)
    return '🧹 115 回收站 · 已清空', '\n'.join(lines)
