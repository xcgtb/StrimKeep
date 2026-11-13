const ONGOING = ['Returning Series', 'In Production', 'Planned', 'Pilot']

/** 片库映射卡片状态：口径对齐1.2.12 口径（完整 / 缺集 / 超集 / 在更 / 待对照）。 */
export function inventoryCardStatus(item: any, entry: any): {text: string, tone: string, category: string, hint: string} {
  // 身份冲突优先：同一个 TMDB 号挂了两套不同目录，此时任何缺集/完整结论都不可信。
  if (item.identity_conflict) return {text: '身份冲突', tone: 'unknown', category: 'unknown',
                                      hint: '同一 TMDB 编号对应不同作品目录，请先核对身份'}
  if (item.kind === 'movie') return {text: '已入库', tone: 'ok', category: 'movie', hint: '电影不按集号核对，只比较两库版本'}
  if (!entry) {
    if (item.manual_complete) return {text: '手动完结', tone: 'ok', category: 'complete',
                                      hint: '已手动标记完结，没做 TMDB 对照'}
    return {text: '待对照', tone: 'pending', category: 'pending', hint: '还没有做 TMDB 集数对照'}
  }
  if (entry.loading) return {text: '对照中', tone: 'pending', category: 'pending', hint: '正在读取 TMDB 集数'}
  if (entry.error) return {text: '对照失败', tone: 'unknown', category: 'pending', hint: 'TMDB 读取失败：' + entry.error}
  const data = entry.data
  if (!data?.tmdb_complete || !Array.isArray(data.seasons) || !data.seasons.length) {
    const failed = (data?.failed_seasons || []).filter((n: number) => n > 0)
    return {text: '待对照', tone: 'pending', category: 'pending',
            hint: failed.length ? 'TMDB 第 ' + failed.join('、') + ' 季取不到；其余季仍按已播集统计'
                                : 'TMDB 没有给出季/集信息'}
  }
  const ongoing = ONGOING.includes(data.series_status)
  const suffix = data.inventory_complete ? '' : ' · 库存待核实'
  if (item.manual_complete && data.combined_missing > 0) return {text: '✓ 完整', tone: 'ok', category: 'complete', hint: '已手动标记完结，实际缺集仍可在详情查看'}
  // 库存扫描有未核实记录时，缺集数字本身不可信：归类为 unknown，由界面提示「库存待核实」。
  const trusted = data.inventory_complete !== false
  if (data.combined_missing > 0)
    return {text: (ongoing ? '在更 · ' : '') + '缺 ' + data.combined_missing + ' 集' + suffix,
            tone: trusted ? (ongoing ? 'ongoing' : 'missing') : 'unknown',
            category: trusted ? (ongoing ? 'ongoing' : 'missing') : 'unknown',
            hint: ongoing ? '连载中：只统计已播集，未播集不算缺' : 'TMDB 已播集里有没入库的'}
  if (ongoing && data.seasons.some((s: any) => s.expected?.length)) return {text: '✓ 完整' + suffix, tone: 'ok', category: 'complete', hint: '连载中，已播集齐全'}
  if (data.combined_extra > 0)
    return {text: '超 ' + data.combined_extra + ' 集' + suffix,
            tone: trusted ? 'extra' : 'unknown', category: trusted ? 'extra' : 'unknown',
            hint: '比 TMDB 已播集多，可能是未播集已入库或集号错位'}
  if (!data.seasons.some((s: any) => s.expected?.length))
    return {text: '待对照', tone: 'pending', category: 'pending', hint: '该剧还没有已播集信息'}
  return {text: '✓ 完整' + suffix, tone: 'ok', category: 'complete', hint: 'TMDB 已播集全部入库'}
}

/** 状态图例，页面上直接展示，避免"待核实"这类说不清的字眼。 */
export const STATUS_LEGEND = [
  {text: '✓ 完整', hint: 'TMDB 已播集全部入库'},
  {text: '缺 N 集', hint: 'TMDB 已播集里还有没入库的'},
  {text: '超 N 集', hint: '比 TMDB 已播集多，可能是未播集或集号错位'},
  {text: '在更', hint: '连载中，只统计已播集'},
  {text: '待对照', hint: '还没拿到 TMDB 季/集信息'},
]
