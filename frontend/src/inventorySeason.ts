export type LibrarySeasonStatus = {label:string, tone:string, percent:number|null, airedPresent:number}

/** Calculate status from episode identities, not just file or episode totals. */
export function librarySeasonStatus(entry:any, side:string, inventoryComplete:boolean):LibrarySeasonStatus {
  const data = entry?.[side] || {}
  const expected:number[] = Array.isArray(entry?.expected) ? entry.expected : []
  const present = new Set<number>(Array.isArray(data.present) ? data.present : [])
  const missing:number[] = Array.isArray(data.missing) ? data.missing : []
  const extra:number[] = Array.isArray(data.extra) ? data.extra : []
  const airedPresent = expected.filter(ep => present.has(ep)).length
  const officialKnown = entry?.official_known === true
  // unknown_air_date 是「连载中还没公布播出日期」的集，属正常情况：它们本来就不计入
  // 缺集统计，页面上另有「播出日期未知：…」的说明。把它当「待核实」会让一部已播集
  // 全齐的剧（如 走马上任 27/27）也显示待核实，用户无从判断。
  const uncertain = !inventoryComplete || !officialKnown || !expected.length
  const parts = [missing.length ? `缺 ${missing.length} 集` : '', extra.length ? `超 ${extra.length} 集` : ''].filter(Boolean)
  if (uncertain) parts.push('待核实')
  return {label: parts.join(' · ') || '完整', tone:uncertain ? 'unknown' : missing.length ? 'missing' : extra.length ? 'extra' : 'complete',
          percent:officialKnown && expected.length ? Math.round(airedPresent * 100 / expected.length) : null, airedPresent}
}

export function ordinaryEpisodes(data:any):number[] {
  const extra = new Set<number>(Array.isArray(data?.extra) ? data.extra : [])
  return (Array.isArray(data?.present) ? data.present : []).filter((episode:number) => !extra.has(episode))
}
