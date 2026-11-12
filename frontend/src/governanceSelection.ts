/** Prevent mutually exclusive cleanup actions from being selected together. */
export function actionKey(row:any):string {
  return JSON.stringify([row.actionType, row.kind, row.tmdb_id, row.season ?? null,
    row.episode ?? null, row.deleteSide, row.reason ?? null, row.action_reason ?? null])
}

export function selectionConflict(row:any, selected:any[]):string {
  for (const other of selected) {
    if (actionKey(row) === actionKey(other)) continue
    if (row.kind !== other.kind || row.tmdb_id !== other.tmdb_id || row.season !== other.season) continue
    const bothGaps = row.reason === 'episode_gap' && other.reason === 'episode_gap'
    const bothDual = row.actionType === 'dual' && other.actionType === 'dual'
    const explicitS00 = bothDual && row.kind === 'tv' && row.season === 0 &&
      [row.action_reason, other.action_reason].includes('special_delete_local') &&
      [row.action_reason, other.action_reason].includes('special_delete_share')
    if (!explicitS00 && (bothGaps || bothDual) && row.deleteSide !== other.deleteSide)
      return `同季已选择${other.deleteSide === 'cloud' ? '网盘库' : '分享库'}清理；请先处理一方并重新扫描，避免两库同时清空。`
    if (row.deleteSide === other.deleteSide && row.actionType === 'single' && other.actionType === 'single'
        && (row.reason === 'episode_gap' || other.reason === 'episode_gap'))
      return '缺口季清理包含该季全部文件，与同库其他清理项重叠；请分开处理并重新扫描。'
  }
  return ''
}

/** Do not choose an arbitrary side when bulk selection encounters conflicts. */
export function bulkConflictKeys(rows:any[]):Set<string> {
  return new Set(rows.filter(row => selectionConflict(row, rows)).map(actionKey))
}

export function oneClickCandidates(rows:any[], singleQuality:boolean, singleGaps:boolean):any[] {
  const enabled = rows.filter(row => row.actionType !== 'single' ||
    (row.reason !== 'quality' || singleQuality) && (row.reason !== 'episode_gap' || singleGaps))
  const conflicts = bulkConflictKeys(enabled)
  return enabled.filter(row => !conflicts.has(actionKey(row)))
}
