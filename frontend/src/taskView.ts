// 任务中心统一视图：kind 标签与 source 归类只在这里定义。
//
// 后端 job_public 输出原始 state/config/counts；前端不再各自从
// config.origin / transfer_parent / scheduled 猜来源，也不必在每个
// 页面重复维护 kind 中文映射。

export const TASK_KINDS: Record<string, string> = {
  transfer: '115 转存',
  organize: '扫描整理',
  share_organize: '分享 STRM 整理',
  archive_index: '归档索引',
  strm_sync: 'STRM 同步',
  classification_migration: '历史分类迁移（已停用）',
}

export function taskKindLabel(kind: string): string {
  return TASK_KINDS[kind] || kind
}

export function taskSourceLabel(job: any): string {
  const config = job.config || {}
  const origin = String(config.origin || '')
  if (origin === 'manual_import') return '手动匹配'
  if (origin.startsWith('offline:')) return '离线下载'
  if (config.transfer_parent) return '转存后整理'
  if (origin === 'channel') return '频道监控'
  if (origin === 'telegram') return 'Telegram'
  return config.scheduled ? '定时' : '手动'
}
