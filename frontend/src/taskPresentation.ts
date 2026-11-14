type Task = {
  kind: string
  state: string
  stage?: string
  counts: Record<string, number>
  total: number
  can_retry_cleanup?: boolean
  unidentified?: number
  filtered?: number
  filtered_items?: number
  notify_state?: string
  scan_filter?: {scanned?:number;excluded?:number;attachments?:number;formats?:Record<string,number>;reasons?:Record<string,number>}
  message?: string
  sync_mode?: string
  emby?: {state:string;message:string;confirmed:number;total:number;attempts:number}
}

const states: Record<string, string> = {
  queued: '排队中', running: '执行中', paused: '已暂停',
  waiting_login: '需要重新登录 115', needs_review: '需要核查',
  cancelled: '已取消', completed: '完成', partial: '有项目需要处理',
}

type TagType = 'success' | 'info' | 'warning' | 'danger'

export function taskStatus(job: Task): {label: string; type: TagType} {
  const stage = String(job.stage || '')
  const state = job.state
  if (state === 'completed') return {label: '成功', type: 'success'}
  if (state === 'partial') return {label: '部分完成', type: 'warning'}
  if (state === 'cancelled') return {label: '已取消', type: 'info'}
  if (state === 'queued') return {label: '排队中', type: 'info'}
  if (state === 'running') return {label: '进行中', type: 'warning'}
  if (state === 'paused') return {label: stage.startsWith('让路') ? '让路给转存' : '已暂停', type: 'warning'}
  if (state === 'waiting_login') return {label: '等待登录', type: 'warning'}
  if (state === 'needs_review') {
    // 限流冷却和需要重新登录都不是「失败」，别混在一起。
    if (stage.includes('限流')) return {label: '限流冷却', type: 'warning'}
    return {label: '失败待处理', type: 'danger'}
  }
  return {label: state, type: 'info'}
}

export function taskPresentation(job: Task) {
  const counts = job.counts || {}
  const done = counts.done || 0
  const ended = ['completed', 'partial'].includes(job.state)
  let title = states[job.state] || job.state
  let summary = ''
  const notes: string[] = []
  if (job.kind === 'organize') {
    summary = [
      done ? `已整理 ${done} 个视频` : '',
      counts.pending ? `${counts.pending} 个文件待处理` : '',
      counts.review ? `${counts.review} 个文件未完成` : '',
      counts.recycled ? `已回收 ${counts.recycled} 个重复副本` : '',
      counts.skipped ? `已跳过 ${counts.skipped} 个视频` : '',
    ].filter(Boolean).join(' · ')
    if (job.state === 'completed') title = job.total ? '处理完成' : '未发现待整理视频'
    if (job.state === 'completed' && done) title = '整理完成'
    if (job.state === 'completed' && !job.total) summary = '本次扫描没有发现可处理的视频。'
    if (job.state === 'completed' && !job.total && job.scan_filter?.scanned) {
      title = '已按格式规则跳过'
      summary = job.message || '发现未参与整理的格式或附属文件；已保留原文件，无需人工核实。'
    }
    // 过滤明细按真实原因显示。写死「未列入整理扩展名」会把「小于 100MB 不整理」
    // 也说成扩展名问题，用户看不出到底被哪条规则挡掉。
    const excluded = job.scan_filter?.excluded || 0
    const reasons: Record<string, number> = job.scan_filter?.reasons || {}
    const reasonText = Object.entries(reasons).map(([text, n]) => `${text}（${n} 个）`).join('；')
    if (excluded) notes.push(`另有 ${excluded} 个文件被整理规则过滤：${reasonText || '扩展名/体积/关键词不符合'}。` + (job.total ? '已保留原文件。' : ''))
    const filteredItems = job.filtered_items || 0
    if (filteredItems > 0 && !excluded) notes.push(`另有 ${filteredItems} 个文件按洗版规则保留已有（已有更好版本、同名条目或不符合整理规则），原文件未改动。`)
    if (job.can_retry_cleanup) {
      title = counts.pending ? '部分文件已转未识别目录' : '视频处理完成'
      notes.push('视频处理结果已保留；空目录暂留，不影响后续整理。')
    } else if (job.state === 'partial' && !counts.pending && !['failed', 'unknown'].includes(job.notify_state || '')) {
      notes.push('有步骤尚未完成，请打开处理结果查看原因。')
    }
  } else if (job.kind === 'transfer') {
    if(ended) title=job.state==='completed'?'转存完成':'转存有项目需处理'
    summary='展开任务查看转存结果及后续整理进度。'
  } else if (job.kind === 'share_organize') {
    if (ended) title = job.state === 'completed' ? '分享整理完成' : '分享整理有项目需处理'
    summary = [
      done ? `入库 ${done} 个` : '',
      counts.skipped ? `跳过 ${counts.skipped} 个` : '',
      counts.review ? `未识别 ${counts.review} 个` : '',
    ].filter(Boolean).join(' · ')
  } else if (job.kind === 'strm_sync') {
    if (ended) title = job.state === 'completed' ? 'STRM 同步完成' : 'STRM 同步有项目需处理'
    if (done) summary = job.sync_mode === 'cached' ? `复用近期清单，已检查 ${done} 个视频的本地输出` : `已核对 ${done} 个视频的 STRM 与字幕`
    if (counts.skipped) summary += `${summary ? ' · ' : ''}${counts.skipped} 个文件未更新`
  } else if (job.kind === 'archive_index') {
    summary = `已确认 ${counts.indexed || 0} 个视频`
    if (counts.unresolved) summary += ` · ${counts.unresolved} 个身份待确认`
  }
  if (job.state === 'needs_review') notes.push('本任务已中断，未完成的文件保持原样；后续任务照常执行。展开技术详情可看原因。')
  if (ended && job.notify_state === 'failed') notes.push('Emby 路径通知发送失败。')
  if (ended && job.notify_state === 'unknown') notes.push('Emby 路径通知的结果尚未确认。')
  if (ended && job.notify_state === 'partial') notes.push('Emby 已接受通知，但只有部分 STRM 确认入库。')
  if (job.emby) notes.push(job.emby.message + (job.emby.total ? `（已确认 ${job.emby.confirmed}/${job.emby.total}）` : ''))
  return {title, summary, notes}
}
