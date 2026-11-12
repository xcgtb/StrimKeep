export type ScheduleKind = 'scan' | 'sync'
export type ScheduleDraft = { enabled: boolean; minutes: number; mode:'interval'|'cron'; cron:string; timezone:string; source_cids: string[]; directory_labels: Record<string,string> }
export function scheduleDraft(config: Record<string,any>, kind: ScheduleKind): ScheduleDraft {
  return { mode:config[`${kind}_mode`]||'interval',cron:config[`${kind}_cron`]||(kind==='sync'?'0 3 * * *':'*/5 * * * *'),timezone:config.timezone||'Asia/Shanghai',enabled: !!config[`${kind}_enabled`], minutes: config[`${kind}_interval_minutes`] ?? 60,
    source_cids: kind === 'sync' ? [...(config.sync_source_cids || [])] : [], directory_labels: {...(config.directory_labels || {})} }
}
export function mergeSchedule(current: Record<string,any>, kind: ScheduleKind, draft: ScheduleDraft) {
  const result = {...current,[`${kind}_mode`]:draft.mode||'interval',[`${kind}_cron`]:draft.cron||(kind==='sync'?'0 3 * * *':'*/5 * * * *'),timezone:draft.timezone||'Asia/Shanghai', [`${kind}_enabled`]: draft.enabled, [`${kind}_interval_minutes`]: draft.minutes}
  if(kind === 'sync') { result.sync_source_cids = [...draft.source_cids]; result.directory_labels = {...current.directory_labels,...Object.fromEntries(draft.source_cids.filter(id=>draft.directory_labels[id]).map(id=>[id,draft.directory_labels[id]]))} }
  return result
}
export function scheduleIdentity(draft: ScheduleDraft, kind: ScheduleKind) {
  return JSON.stringify({enabled:draft.enabled,minutes:draft.minutes,mode:draft.mode,cron:draft.cron,timezone:draft.timezone,...(kind==='sync'?{source_cids:draft.source_cids,directory_labels:draft.directory_labels}:{})})
}

export function schedulePatch(kind:ScheduleKind,draft:ScheduleDraft) {
  if(kind==='scan') return {scan_enabled:draft.enabled,scan_interval_minutes:draft.minutes,scan_mode:draft.mode,scan_cron:draft.cron,timezone:draft.timezone}
  return {sync_mode:draft.mode,sync_cron:draft.cron,timezone:draft.timezone,sync_enabled:draft.enabled,sync_interval_minutes:draft.minutes,sync_source_cids:[...draft.source_cids],directory_labels:Object.fromEntries(draft.source_cids.filter(id=>draft.directory_labels[id]).map(id=>[id,draft.directory_labels[id]]))}
}

// The webhook token is saved independently; other draft changes must stay dirty.
export function syncBaselineWithWebhookToken(baseline:string,token:string) {
  const saved=JSON.parse(baseline)
  return JSON.stringify({...saved,config:{...saved.config,webhook_token:token}})
}
