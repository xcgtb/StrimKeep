<script setup lang="ts">
import {computed, nextTick, onMounted, onUnmounted, ref, watch} from 'vue'
import {useRouter} from 'vue-router'
import {ElMessage, ElMessageBox} from 'element-plus'
import {inventoryCardStatus, STATUS_LEGEND} from './inventoryStatus'
import {actionKey, bulkConflictKeys, selectionConflict} from './governanceSelection'
import {librarySeasonStatus, ordinaryEpisodes} from './inventorySeason'
import {usePageActivity} from './pageActivity'

const props = defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const router = useRouter()
const busy = ref(false), error = ref(''), search = ref(''), snapshot = ref<any>(null)
const selected = ref<any>(null), season = ref(1), seasonResult = ref<any>(null), seasonBusy = ref(false), seasonError = ref(''), detailError = ref('')
const missingResult = ref<any>(null), missingBusy = ref(false), missingError = ref('')
let missingRequest = 0, inspectRequest = 0
const tab = ref('mapping'), tie = ref<'cloud'|'share'>('cloud'), preview = ref<any>(null), previewBusy = ref(false), previewError = ref('')
const decision = ref<'quality_first'|'keep_local'|'keep_share'>('quality_first')
const multiProtect = ref<'compare'|'off'>('compare'), specialAction = ref<'compare'|'delete'|'ignore'>('compare'), exemptInput = ref('')
const exemptKeywords = () => [...new Set(exemptInput.value.split(/[,，\n]/).map(s => s.trim()).filter(Boolean))]
const singleQuality = ref(false), singleGaps = ref(false)
const matchStrategy = ref('title_year')
const scanScheduleEnabled = ref(false), scanIntervalHours = ref(6), scanTime = ref(0)
const seasonRatio = ref(90), quietMinutes = ref(15), ruleInfo = ref<any>(null), ruleError = ref('')
const savedSettings = ref('')
const settingsKey = () => JSON.stringify({tie:tie.value, decision:decision.value, match_strategy:matchStrategy.value, multi_season_protect:multiProtect.value, special_action:specialAction.value, exempt_keywords:exemptKeywords(), single_quality:singleQuality.value, single_gaps:singleGaps.value, season_ratio:seasonRatio.value / 100, quiet_minutes:quietMinutes.value, scan_schedule_enabled:scanScheduleEnabled.value, scan_interval_hours:scanIntervalHours.value})
const settingsDirty = computed(() => !savedSettings.value || savedSettings.value !== settingsKey())
const configBusy = ref(false), configMessage = ref('')
const completeBusy = ref(false), completeError = ref('')
const audit = ref<any>(null), auditBusy = ref(false), auditError = ref('')
const auditPanel = ref<HTMLDetailsElement | null>(null)
const stale = ref<any[]>([]), staleBusy = ref(false), staleError = ref(''), staleBackup = ref('')
const history = ref<any[]>([]), historyError = ref('')
const chosen = ref<any[]>([]), plan = ref<any>(null), planOpen = ref(false), planBusy = ref(false), planError = ref(''), execution = ref<any>(null)
const refreshAfterDeleteBusy = ref(false)
let refreshAfterDeleteGeneration = 0
const governanceSelected = ref<any[]>([])
const governanceFilter = ref('all')
const selectionNote = ref('')
const governanceActions = computed(() => [
  ...(preview.value?.dual || []).map((row:any) => ({...row, actionType:'dual', deleteSide:row.action_kind === 'loc' ? 'cloud' : 'share', fileCount:actionCount(row)})),
  ...(preview.value?.single || []).filter((row:any) => ['quality','episode_gap','covered_episode'].includes(row.reason)).map((row:any) => ({...row, actionType:'single', deleteSide:row.side, fileCount:row.reason === 'quality' ? ((row.losers || []).length || Math.max(0,(row.files || []).length - 1)) : (row.files || []).length}))
])
const visibleGovernanceActions = computed(() => governanceActions.value.filter(row => governanceFilter.value === 'all' || row.deleteSide === governanceFilter.value))
function selectedAction(row:any) { return governanceSelected.value.some(value => actionKey(value) === actionKey(row)) }
function actionDisabledReason(row:any) {
  if (!preview.value?.scan_complete) return '库存未完整核实，请先处理扫描问题。'
  if (settingsDirty.value) return '设置已更改，请保存并重新扫描。'
  if (row.actionType === 'dual' && row.incomplete_sides?.length && !singleGaps.value) return '该季存在单库缺集，请先开启并保存缺集季清理。'
  if (row.actionType === 'single' && row.reason === 'quality' && !singleQuality.value) return '请先开启并保存单库多版本清理。'
  if (row.actionType === 'single' && row.reason === 'episode_gap' && !singleGaps.value) return '请先开启并保存缺口季清理。'
  return selectedAction(row) ? '' : selectionConflict(row, governanceSelected.value)
}
function toggleGovernanceAction(row:any, on:boolean) {
  if (on && actionDisabledReason(row)) return
  governanceSelected.value = on ? [...governanceSelected.value, row] : governanceSelected.value.filter(value => actionKey(value) !== actionKey(row))
  selectionNote.value = ''
}
function clearGovernanceSelection() { governanceSelected.value = []; selectionNote.value = '' }
function selectAvailableActions() {
  let skipped = 0
  const conflicts = bulkConflictKeys(governanceActions.value.filter(row => !selectedAction(row)))
  for (const row of visibleGovernanceActions.value) {
    if (selectedAction(row)) continue
    if (conflicts.has(actionKey(row)) || actionDisabledReason(row)) { skipped++; continue }
    governanceSelected.value = [...governanceSelected.value, row]
  }
  selectionNote.value = skipped ? `已跳过 ${skipped} 项：同季两库清理互斥、文件重叠或对应开关未开启。请展开查看原因。` : '已选择当前可处理项。'
}
const kindFilter = ref('all'), scopeFilter = ref('all'), statusFilter = ref('all'), categoryFilter = ref('all'), page = ref(1), pageSize = ref(window.innerWidth < 640 ? 30 : 45)
const statusById = ref<Record<string,any>>({}), statusBusy = ref(false), statusProgress = ref('')
const embyItems = ref<any[]>([]), embyError = ref(''), embyBusy = ref(false)
const orphanBusy = ref(false), orphanError = ref(''), cloudOrphanError=ref(''), orphanResult = ref<any>(null), orphanSelected = ref<any[]>([]), orphanDone = ref<any[]>([])
const orphanJobs=ref<any[]>([]), orphanJob=ref<any>(null)
let orphanTimer:ReturnType<typeof setTimeout>|undefined, orphanVisible=false
let statusGeneration = 0, embyGeneration = 0
const statusInFlight = new Map<string, Promise<void>>()
watch([tie, decision, matchStrategy, multiProtect, specialAction, exemptInput, singleQuality, singleGaps, seasonRatio, quietMinutes, scanScheduleEnabled, scanIntervalHours], () => { preview.value = null; scanTime.value = 0; configMessage.value = '' })
const reasons:Record<string,string> = {recent_activity:'近期仍有入库变动，暂不治理', activity_unknown:'目录变动时间无法核实', missing_or_unsafe:'文件缺失或路径不安全', unsafe_path:'路径不安全', duplicate_path:'多个记录使用同一路径', identity_unresolved:'TMDB 身份未能核实', identity_conflict:'同一 TMDB 编号对应不同片名或年份', episode_ambiguous:'集号有歧义', episode_unresolved:'集号未能识别', episode_invalid:'集号范围无效', audit_mismatch:'目录和台账不一致，请先核对', source_unverified:'115 账号或文件来源身份无法核实，请检查登录和台账', inventory_incomplete:'库存有无法核实的记录', single_library_duplicates:'单库存在多版本', single_library_not_clean:'单库尚有多版本或缺口', episode_gap:'前序缺失或中间跳集', merged_episodes:'合并集需人工核实', unknown_quality:'质量信息不足', quality_rules_disabled:'未启用有效画质比较规则', mixed_quality_or_coverage:'画质结果或集数覆盖不足', tie_or_conflict:'平局或结果冲突', incomplete:'季集不连续', coverage_conflict:'双方集数无法互相覆盖',multi_overlap_protected:'双方各有对方没有的季，同季不动'}
function label(row:any) { return `${row.title || '全部库存'}${row.season != null ? row.season === 0 ? ' · 特别篇（第 0 季）' : ` · 第 ${row.season} 季` : ''}${row.episode ? ` · 第 ${row.episode} 集` : ''}` }
function side(value:string) { return value === 'cloud' ? '网盘库' : value === 'share' ? '分享库' : value === 'cloud115' ? '115 网盘' : '待核实' }
function reason(value:string) { return reasons[value] || value }
const skipReasons:Record<string,string> = {symlink:'软链接目录', has_media_or_unknown:'含视频/未知文件',
  changed:'扫描时变动', too_large:'过大过深', not_plain_directory:'不是普通目录', no_tmdb:'无 TMDB 标识',
  unreadable:'读取失败', invalid:'配置不合法'}
function skipReason(code:string) { return skipReasons[code] || code }
function deleteLabel(row:any) { return row.action_kind === 'loc' ? '删本地' : '删分享' }
function actionCount(row:any) {
  const files = row.action_kind === 'loc' ? row.cloud_files : row.share_files
  return (files || []).length
}
async function scanOrphans() {
  orphanBusy.value = true; orphanError.value = ''; cloudOrphanError.value = ''; orphanResult.value = null; orphanSelected.value = []; orphanDone.value = []
  try {
    const local=await props.api('/inventory/orphans/scan', 'POST')
    let remote:any={items:[],skipped:0}
    try { remote=await props.api('/inventory/cloud-orphans/scan','POST') }
    catch(e){cloudOrphanError.value='115 网盘扫描未完成，本次只能清理本地：'+(e as Error).message}
    const byPath=new Map<string,any>(remote.items.map((row:any)=>[row.local_path,row]))
    const linked=new Set<string>()
    const rows=local.items.map((row:any)=>{const found=row.side==='cloud'&&row.kind!=='file'?byPath.get(row.path):null;if(found){linked.add(found.id);return {...row,linkedCloud:found}}return row})
    for(const row of remote.items)if(!linked.has(row.id))rows.push({side:'cloud115',kind:'cloud_directory',path:row.path,files:row.files,dirs:row.dirs,linkedCloud:row})
    orphanResult.value={...local,items:rows,cloudAttentionCount:remote.attention_count||0,cloudAttentionItems:remote.attention_items||[]}
  } catch (e) { orphanError.value = (e as Error).message }
  finally { orphanBusy.value = false }
}
async function loadOrphanJobs() {
  try {
    const response=await props.api('/inventory/orphans/jobs')
    orphanJobs.value=response.items || []
    const active=orphanJobs.value.find((job:any)=>['queued','running'].includes(job.state))
    if(active) { orphanJob.value=active; orphanBusy.value=true; scheduleOrphanPoll() }
    else { if(orphanJob.value) orphanJob.value=orphanJobs.value.find((job:any)=>job.id===orphanJob.value.id) || null; orphanBusy.value=false }
  } catch(e) { orphanError.value='清理记录读取失败：'+(e as Error).message; if(orphanBusy.value) scheduleOrphanPoll() }
}
function scheduleOrphanPoll() { if(orphanTimer) clearTimeout(orphanTimer); if(orphanVisible) orphanTimer=setTimeout(loadOrphanJobs,3000) }
function jobState(state:string) { return ({queued:'等待执行',running:'清理中',completed:'已完成',partial:'部分完成',interrupted:'已中断'} as Record<string,string>)[state] || state }
async function cleanOrphans() {
  if (!orphanSelected.value.length) return
  const cloudCount=orphanSelected.value.filter(row=>row.linkedCloud).length
  try { await ElMessageBox.confirm(`将清理选中的 ${orphanSelected.value.length} 项；其中 ${cloudCount} 项会联动回收 115 网盘的字幕、元数据和空目录。分享侧只清理本地。执行前会再次核对。`, '确认清理残留', {type:'warning', confirmButtonText:'确认清理', cancelButtonText:'取消'}) } catch { return }
  orphanBusy.value=true; orphanError.value=''; orphanDone.value=[]
  const id=Array.from(crypto.getRandomValues(new Uint8Array(16))).map(x=>x.toString(16).padStart(2,'0')).join('')
  const items=orphanSelected.value.map(row=>({
    local:row.kind==='cloud_directory'?null:{path:row.path,fingerprint:row.fingerprint,kind:row.kind||'directory',source:row.source||''},
    cloud:row.linkedCloud?{id:row.linkedCloud.id,fingerprint:row.linkedCloud.fingerprint}:null
  }))
  orphanResult.value=null; orphanSelected.value=[]
  try { orphanJob.value=await props.api('/inventory/orphans/jobs','POST',{id,items}); await loadOrphanJobs() }
  catch(e) {
    try { orphanJob.value=await props.api('/inventory/orphans/jobs/'+id); await loadOrphanJobs() }
    catch { orphanBusy.value=false; orphanError.value='提交状态无法确认，请刷新清理记录并重新扫描后再操作：'+(e as Error).message }
  }
}
function episodeList(values:any[]) { return values?.length ? values.map((ep:number) => `E${String(ep).padStart(2,'0')}`).join('、') : '无' }
function ruleValue(value:any) { if (value == null) return '未知'; return typeof value === 'object' ? Object.values(value).filter(v => v != null && v !== '').join(' / ') || '未知' : String(value) }
const rows = ref<any[]>([]), mappingTotal = ref(0), pageBusy = ref(false), pageFallback = ref(false)
let pageRequest = 0
const inventoryComplete = ref(true), inventoryIssues = ref<any[]>([]), inventoryIssueCount = ref(0)
const totalPages = computed(() => Math.max(1, Math.ceil(mappingTotal.value / pageSize.value)))
const batchMode = ref(false)
let swipeState:any = null
let swipeSuppressClickUntil = 0
function workItem(key:string) { return visibleItems.value.find((row:any) => workKey(row) === key) }
function markAt(x:number, y:number, paint:boolean) {
  if (typeof document.elementFromPoint !== 'function') return
  const node = document.elementFromPoint(x, y) as HTMLElement | null
  const card = node?.closest ? node.closest('.poster-card') as HTMLElement | null : null
  if (!card) return
  const item = workItem(card.dataset.workKey || '')
  if (item && picked(item) !== paint) togglePicked(item, paint)
}
function wallTouchStart(event:TouchEvent) {
  swipeState = null
  if (!batchMode.value || event.touches.length !== 1) return
  const target = event.target as HTMLElement
  if (target.closest && target.closest('label.poster-pick')) return
  const card = target.closest ? target.closest('.poster-card') as HTMLElement : null
  if (!card) return
  const touch = event.touches[0]
  const start = workItem(card.dataset.workKey || '')
  // 起点已选中的话，这一笔就是「反向取消」，否则是「勾选」。
  swipeState = {x: touch.clientX, y: touch.clientY, lastX: touch.clientX, lastY: touch.clientY,
                mode: '', paint: start ? !picked(start) : true}
}
function wallTouchMove(event:TouchEvent) {
  if (!swipeState || event.touches.length !== 1 || !batchMode.value) return
  const touch = event.touches[0]
  const dx = touch.clientX - swipeState.x, dy = touch.clientY - swipeState.y
  // 只有明显的横向手势才连续勾选，竖向留给页面滚动。
  if (!swipeState.mode && Math.hypot(dx, dy) > 12) swipeState.mode = Math.abs(dx) > Math.abs(dy) * 1.25 ? 'select' : 'scroll'
  if (swipeState.mode !== 'select') return
  if (event.cancelable) event.preventDefault()
  swipeSuppressClickUntil = Date.now() + 800
  const steps = Math.min(24, Math.max(1, Math.ceil(Math.abs(touch.clientX - swipeState.lastX) / 18)))
  for (let index = 0; index <= steps; index += 1) {
    const fraction = steps ? index / steps : 1
    markAt(swipeState.lastX + (touch.clientX - swipeState.lastX) * fraction,
           swipeState.lastY + (touch.clientY - swipeState.lastY) * fraction, swipeState.paint)
  }
  markAt(swipeState.x, swipeState.y, swipeState.paint)
  swipeState.lastX = touch.clientX; swipeState.lastY = touch.clientY
}
function wallTouchEnd() {
  if (swipeState && swipeState.mode === 'select') swipeSuppressClickUntil = Date.now() + 800
  swipeState = null
}
function selectPage() {
  for (const item of visibleItems.value) if (!picked(item)) togglePicked(item, true)
}
function unselectPage() {
  for (const item of visibleItems.value) if (picked(item)) togglePicked(item, false)
}
function toggleBatchMode() {
  batchMode.value = !batchMode.value
  if (!batchMode.value) chosen.value = []
}
const visibleItems = computed(() => rows.value)
function updatePageSize() { pageSize.value = window.innerWidth < 640 ? 30 : 45; page.value = Math.min(page.value, totalPages.value) }
// 卡片只带"在哪几库/各有几个文件"，统一成模板用的字段；详情打开时再取完整文件清单。
const allCategories = ref<{name:string, count:number}[]>([])
const categorySource = ref('path')
const categories:Record<string,string[]> = {'儿童节目':['儿童'], '演唱会':['演唱会'], '综艺剧':['综艺'], '动漫剧':['动漫'], '动画电影':['动画电影'], '纪录片':['纪录'], '国产剧':['国产剧集','国产剧'], '欧美剧':['欧美剧集','欧美剧','其他剧集'], '日韩剧':['日韩剧集','日韩剧'], '华语电影':['国产电影','华语电影'], '外语电影':['欧美电影','日韩电影','外语电影','其他电影']}
function itemCategories(files:any[]) {
  const found = new Set<string>()
  for (const file of files) for (const part of String(file.path || '').replaceAll('\\','/').split('/')) {
    if (/\{tmdb-\d+\}/i.test(part)) break
    const category = Object.entries(categories).find(([,words]) => words.some(word => part.includes(word)))
    if (category) { found.add(category[0]); break }
  }
  return found.size ? [...found] : ['未分类']
}
function withFiles(item:any) {
  const files = (row:any[]|undefined) => row || []
  const cloud = files(item.cloud), share = files(item.share)
  return {...item, cloud, share, categories:item.categories ?? itemCategories([...cloud,...share]),
    in_cloud: item.in_cloud ?? cloud.length > 0, in_share: item.in_share ?? share.length > 0,
    cloud_files: item.cloud_files ?? cloud.length, share_files: item.share_files ?? share.length}
}
function localFilter(list:any[]) {
  const keyword = search.value.trim().toLowerCase()
  return list.filter((item:any) =>
    (!keyword || item.title.toLowerCase().includes(keyword) || String(item.tmdb_id).includes(keyword)) &&
    (kindFilter.value === 'all' || item.kind === kindFilter.value) &&
    (scopeFilter.value === 'all' || (scopeFilter.value === 'both' ? item.in_cloud && item.in_share
      : scopeFilter.value === 'cloud' ? item.in_cloud : item.in_share)) &&
    (categoryFilter.value === 'all' || item.categories?.includes(categoryFilter.value)) &&
    (statusFilter.value === 'all' || inventoryCardStatus(item, statusById.value[workKey(item)]).category === statusFilter.value))
}
async function loadPage(refresh=false) {
  const request = ++pageRequest
  pageBusy.value = true; error.value = ''
  const query = new URLSearchParams({media:kindFilter.value, status:statusFilter.value,
    scope:scopeFilter.value, category:categoryFilter.value, keyword:search.value.trim(),
    offset:String((page.value - 1) * pageSize.value), limit:String(pageSize.value),
    refresh:refresh ? '1' : '0'})
  try {
    // 1.2.12 口径：过滤/计数/排序都在服务端算完再切片，卡片不含任何路径。
    const data = await props.api(`/inventory/mapping/page?${query}`)
    if (request !== pageRequest) return
    pageFallback.value = false
    rows.value = (data.rows || []).map(withFiles)
    if (Array.isArray(data.all_categories)) allCategories.value = data.all_categories
    categorySource.value = data.category_source || 'path'
    mappingTotal.value = Number(data.total || 0)
    inventoryComplete.value = data.inventory_complete !== false
    inventoryIssues.value = data.issues || []
    inventoryIssueCount.value = data.issue_count ?? inventoryIssues.value.length
    if (data.refresh_error) error.value = data.refresh_error
  } catch (original) {
    if (request !== pageRequest) return
    // 老后端没有映射分页接口：退回整库快照 + 本地过滤分页（行为与升级前一致）。
    pageFallback.value = true
    if (!snapshot.value) snapshot.value = await props.api('/inventory').catch(() => null)
    if (request !== pageRequest) return
    if (!snapshot.value) { error.value = (original as Error).message || '片库读取失败'; inventoryComplete.value = false; return }
    inventoryComplete.value = snapshot.value?.complete !== false
    inventoryIssues.value = snapshot.value?.issues || []
    inventoryIssueCount.value = inventoryIssues.value.length
    const all = (snapshot.value?.items || []).map(withFiles)
    const counts:Record<string, number> = {}
    for (const item of all) for (const name of itemCategories([...(item.cloud || []), ...(item.share || [])])) {
      counts[name] = (counts[name] || 0) + 1
    }
    allCategories.value = Object.keys(counts).map(name => ({name, count: counts[name]}))
    const list = localFilter(all)
    mappingTotal.value = list.length
    rows.value = list.slice((page.value - 1) * pageSize.value, page.value * pageSize.value)
  } finally { if (request === pageRequest) pageBusy.value = false }
  if (request !== pageRequest) return
  void compareItems(visibleItems.value)
}
function workKey(item:any) { return item.unidentified ? item.id : `${item.kind}:${item.tmdb_id}` }
function picked(item:any) { return chosen.value.some(x => workKey(x) === workKey(item)) }
function togglePicked(item:any, on:boolean) { if (item.unidentified) return; chosen.value = on ? [...chosen.value.filter(x => workKey(x) !== workKey(item)), item] : chosen.value.filter(x => workKey(x) !== workKey(item)) }
function cardStatus(item:any) {
  if (item.unidentified) return {text:'未识别', tone:'unknown', category:'unmatched', hint:'文件未匹配 TMDB，暂不参与自动清理'}
  const entry = statusById.value[workKey(item)]
  if (entry || !item.status) return inventoryCardStatus(item, entry)
  if (item.manual_complete && item.status === 'complete') return {text:'✓ 完整', tone:'ok', category:'complete', hint:'已手动标记完结，实际缺集仍可在详情查看'}
  const labels:Record<string,string> = {missing:`缺 ${item.missing} 集`, extra:`超 ${item.extra} 集`, ongoing:'在更', complete:'✓ 完整', unknown:'待对照', pending:'待对照', movie:'已入库'}
  const tones:Record<string,string> = {missing:'missing', extra:'extra', ongoing:'ongoing', complete:'ok', unknown:'pending', pending:'pending', movie:'ok'}
  const hints:Record<string,string> = {missing:'TMDB 已播集里有没入库的', extra:'比 TMDB 已播集多，可能是未播集或集号错位', ongoing:'连载中，只统计已播集', complete:'TMDB 已播集全部入库', unknown:'还没拿到 TMDB 季/集信息', pending:'还没拿到 TMDB 季/集信息', movie:'电影不按集号核对，只比较两库版本'}
  return {text:labels[item.status] || '待对照', tone:tones[item.status] || 'pending', category:item.status, hint:hints[item.status] || '还没拿到 TMDB 季/集信息'}
}
const comparedOnce = new Set<string>()
async function compareItems(list:any[], all=false) {
  const generation = statusGeneration
  // 服务端已判「待对照」的卡片也要重新对照（缓存可能因库存变动失效），
  // 同一会话只自动补一次，避免每次翻页都去问 TMDB。
  const queue = list.filter(item => {
    if (item.kind !== 'tv' || item.unidentified) return false
    if (all) return true
    const key = workKey(item)
    if (!statusById.value[key]) return true
    return item.status === 'pending' && !comparedOnce.has(key)
  })
  if (!queue.length) return
  if (all) { statusBusy.value = true; statusProgress.value = `0 / ${queue.length}` }
  let done = 0; const total = queue.length
  const worker = async () => {
    while (queue.length && generation === statusGeneration) {
      const item = queue.shift(); if (!item) break
      const key = workKey(item)
      let pending = statusInFlight.get(key)
      if (!pending) {
        comparedOnce.add(key)
        statusById.value[key] = {loading:true}
        pending = (async () => {
          try { const data = await props.api(`/inventory/tv/${item.tmdb_id}/missing`); if (generation === statusGeneration) statusById.value[key] = {data} }
          catch (e) { if (generation === statusGeneration) statusById.value[key] = {error:(e as Error).message} }
        })()
        statusInFlight.set(key, pending)
      }
      await pending
      if (statusInFlight.get(key) === pending) statusInFlight.delete(key)
      done++; if (all && generation === statusGeneration) statusProgress.value = `${done} / ${total}`
    }
  }
  try { await Promise.all(Array.from({length:Math.min(3, queue.length)}, worker)) }
  finally { if (all && generation === statusGeneration) statusBusy.value = false }
  if (!all && generation === statusGeneration && statusFilter.value !== 'all') await loadPage()
}
watch([search, kindFilter, scopeFilter, statusFilter, categoryFilter], () => { page.value = 1; chosen.value = []; void loadPage() })
watch([page, pageSize], () => { void loadPage() })
watch(visibleItems, list => { void compareItems(list) })

async function load(refresh=true) {
  busy.value = true; error.value = ''; snapshot.value = null; selected.value = null; seasonResult.value = null; preview.value = null; audit.value = null; statusGeneration++; statusInFlight.clear(); statusById.value = {}; statusBusy.value = false; chosen.value = []; page.value = 1
  try { await loadPage(refresh) }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function loadPreview() {
  if (settingsDirty.value) { previewError.value = '请先保存库存清理设置。'; return }
  previewBusy.value = true; previewError.value = ''; preview.value = null; governanceSelected.value = []
  try { preview.value = await props.api('/inventory/preview', 'POST', {tie:tie.value}); scanTime.value = Date.now() / 1000; if (preview.value.audit) audit.value = preview.value.audit }
  catch (e) { previewError.value = (e as Error).message }
  finally { previewBusy.value = false }
}
async function saveConfig() {
  configBusy.value = true; configMessage.value = ''
  try { await props.api('/inventory/config', 'PUT', JSON.parse(settingsKey())); savedSettings.value = settingsKey(); configMessage.value = '已保存'; preview.value = null }
  catch (e) { configMessage.value = (e as Error).message }
  finally { configBusy.value = false }
}
async function auditLibraries() {
  auditBusy.value = true; auditError.value = ''; audit.value = null
  try { audit.value = await props.api('/inventory/audit', 'POST') }
  catch (e) { auditError.value = (e as Error).message }
  finally { auditBusy.value = false }
}
async function openAuditFromMapping() {
  tab.value = 'governance'
  await nextTick()
  if (auditPanel.value) auditPanel.value.open = true
  void auditLibraries()
}
async function loadHistory() {
  historyError.value = ''
  try { history.value = (await props.api('/inventory-plans')).items || [] }
  catch (e) { historyError.value = (e as Error).message }
}
async function checkStale() {
  staleBusy.value = true; staleError.value = ''; stale.value = []
  try { stale.value = (await props.api('/inventory/stale-records')).items || [] }
  catch (e) { staleError.value = (e as Error).message }
  finally { staleBusy.value = false }
}
async function clearStale() {
  if (!stale.value.length) return
  try { await ElMessageBox.confirm(`将备份并移除 ${stale.value.length} 条已撤销、无归属且本地文件不存在的网盘台账记录；不操作网盘文件。`, '确认修复台账', {type:'warning', confirmButtonText:'备份并修复', cancelButtonText:'取消'}) } catch { return }
  staleBusy.value = true; staleError.value = ''
  try { const result = await props.api('/inventory/stale-records/clear', 'POST', {file_ids:stale.value.map(row => String(row.file_id))}); staleBackup.value = result.backup_id; stale.value = []; await load(); await auditLibraries() }
  catch (e) { staleError.value = (e as Error).message }
  finally { staleBusy.value = false }
}
const planMode = ref('')
const PLAN_LABELS:Record<string, string> = {manual: '手动清理', dual: '双库洗版', single: '单库清理',
  governance: '双库治理'}
const planSide = ref('')
const planScope = computed(() => {
  if (planMode.value !== 'manual') return ''
  return planSide.value === 'cloud' ? '网盘库' : planSide.value === 'share' ? '分享库' : '两库'
})
const planTitle = computed(() => plan.value
  ? `${PLAN_LABELS[planMode.value] || '清理'} · 确认执行`
  : '生成清理清单')
async function prepareDelete(mode:'manual'|'dual'|'single'|'governance', selections:any[]) {
  if (!selections.length) { planError.value = '请先选择作品。'; return }
  planMode.value = mode
  if (planBusy.value) return
  planBusy.value = true; planError.value = ''; execution.value = null; plan.value = null; planOpen.value = true
  try { plan.value = await props.api('/inventory/delete/plan', 'POST', {mode, selections, ...(mode === 'manual' ? {} : {scan_revision:preview.value?.scan_revision})}) }
  catch (e) { planError.value = (e as Error).message }
  finally { planBusy.value = false }
}
function manualDelete(items:any[], side:'cloud'|'share'|'both') {
  planSide.value = side
  void prepareDelete('manual', items.map(item => ({kind:item.kind, tmdb_id:item.tmdb_id, side})))
}
function dualDelete(item:any) {
  void prepareDelete('dual', [{kind:item.kind, tmdb_id:item.tmdb_id, season:item.season ?? null, winner:item.winner, action_kind:item.action_kind, action_reason:item.action_reason || null}])
}
function dualBatch() {
  let skipped = 0
  const candidates = governanceActions.value.filter(item => item.actionType === 'dual')
  const conflicts = bulkConflictKeys(governanceActions.value.filter(row => !selectedAction(row)))
  for (const row of candidates) {
    if (selectedAction(row)) continue
    if (conflicts.has(actionKey(row)) || actionDisabledReason(row)) { skipped++; continue }
    governanceSelected.value = [...governanceSelected.value, row]
  }
  selectionNote.value = skipped ? `已跳过 ${skipped} 项有冲突的双库动作，请查看治理清单。` : '已选择可处理的双库动作，请在治理清单中核对并执行。'
}
function singleDelete(item:any) {
  void prepareDelete('single', [{kind:item.kind, tmdb_id:item.tmdb_id, season:item.season ?? null, episode:item.episode || null, side:item.side, reason:item.reason}])
}
function executeGovernanceSelection() {
  void prepareDelete('governance', governanceSelected.value.map((item:any) => item.actionType === 'dual'
    ? {kind:item.kind, tmdb_id:item.tmdb_id, season:item.season ?? null, winner:item.winner, action_kind:item.action_kind, action_reason:item.action_reason || null}
    : {kind:item.kind, tmdb_id:item.tmdb_id, season:item.season ?? null, episode:item.episode ?? null, side:item.side, reason:item.reason}))
}
function prepareAllGovernance() {
  if (!preview.value?.scan_complete || settingsDirty.value) return
  if (!preview.value.plan) { selectionNote.value = preview.value.plan_error || '本次扫描没有可直接执行的计划。'; return }
  execution.value = null; planError.value = ''; plan.value = preview.value.plan; planOpen.value = true
}
watch(tab, async current => {
  if (current !== 'governance' || settingsDirty.value || previewBusy.value) return
  try {
    const last = await props.api('/inventory/last-scan')
    if (tab.value === 'governance' && !settingsDirty.value && !previewBusy.value && last.result && Number(last.at || 0) > scanTime.value) {
      preview.value = last.result; scanTime.value = Number(last.at)
    }
  } catch { /* 手动扫描仍可用 */ }

})
async function executePlan() {
  if (!plan.value || planBusy.value || execution.value) return
  const blanks = governanceActions.value.filter(row => row.clears_show).length
  try { await ElMessageBox.confirm(`确认按清单清理？网盘文件将进入 115 回收站，分享库只删除本地 STRM。执行后可能需要在 Emby 刷新媒体库。${blanks ? `\n注意：有 ${blanks} 项清理后该季在两库都不再保留。` : ''}`, '确认执行删除', {type:'warning', confirmButtonText:'确认执行', cancelButtonText:'取消'}) }
  catch { return }
  planBusy.value = true; planError.value = ''
  // A lost response may still mean the server accepted the request. Keep
  // this plan non-retryable until its status is checked in the history.
  execution.value = {state:'starting', count:plan.value.actions.length, progress:[]}
  try {
    const id = plan.value.plan_id
    await props.api(`/inventory/delete/${id}/execute`, 'POST')
    for (;;) {
      execution.value = await props.api(`/inventory/delete/${id}`)
      if (!['starting','executing'].includes(execution.value.state)) break
      await new Promise(resolve => setTimeout(resolve, 800))
    }
    if (execution.value.state === 'completed') {
      // The deletion is finished. Reset the old view before yielding so a
      // subsequent selection cannot be cleared by an older refresh.
      preview.value = null; scanTime.value = 0; snapshot.value = null; selected.value = null; seasonResult.value = null
      statusGeneration++; statusInFlight.clear(); statusById.value = {}; statusBusy.value = false
      chosen.value = []; page.value = 1
      const refreshGeneration = ++refreshAfterDeleteGeneration
      refreshAfterDeleteBusy.value = true
      void (async () => {
        try {
          // Let a page-number watcher run first; the forced read below then
          // becomes the newest request and still uses the current filters.
          await nextTick()
          if (refreshGeneration !== refreshAfterDeleteGeneration) return
          await loadPage(true)
          if (refreshGeneration === refreshAfterDeleteGeneration) await loadHistory()
        } finally {
          if (refreshGeneration === refreshAfterDeleteGeneration) refreshAfterDeleteBusy.value = false
        }
      })()
    } else {
      void loadHistory()
    }
  } catch (e) { planError.value = `无法确认执行状态，请查看最近清理计划：${(e as Error).message}`; void loadHistory() }
  finally { planBusy.value = false }
}
function compareSummary(list:any[]) {
  const counts:Record<string, number> = {}
  const stuck:string[] = []
  for (const item of list) {
    const entry = statusById.value[workKey(item)]
    if (!entry || entry.loading) continue
    const status = inventoryCardStatus(item, entry)
    counts[status.category] = (counts[status.category] || 0) + 1
    if (status.category === 'pending') {
      const failed = (entry.data?.failed_seasons || []).filter((n:number) => n > 0)
      stuck.push(failed.length ? `${item.title}（TMDB 缺第 ${failed.join('、')} 季）` : item.title)
    }
  }
  const parts = [['complete', '完整'], ['missing', '缺集'], ['extra', '超集'], ['ongoing', '在更']]
    .filter(([key]) => counts[key]).map(([key, label]) => `${label} ${counts[key]}`)
  const text = parts.length ? '已对照 ' + parts.join(' · ') : '对照完成'
  return stuck.length ? `${text}；仍待对照 ${stuck.length} 部：${stuck.slice(0, 2).join('、')}` : text
}
async function compareAll() {
  const list = visibleItems.value.filter((item:any) => item.kind === 'tv')
  if (!list.length) { ElMessage.info('本页没有电视剧需要对照'); return }
  try {
    await compareItems(list, true)
    const summary = compareSummary(list)
    if (summary.includes('仍待对照')) ElMessage.warning(summary)
    else ElMessage.success(summary)
    await loadPage()
  } catch (e) { error.value = (e as Error).message; ElMessage.error((e as Error).message) }
}
function openWork(row:any) {
  // 治理行 → 片库映射详情：kind/tmdb_id 足以让详情抽屉自己把文件清单取回来。
  tab.value = 'mapping';
  void inspect({kind: row.kind, tmdb_id: row.tmdb_id, title: row.title})
}
async function inspect(item:any) {
  if (Date.now() < swipeSuppressClickUntil) return
  // 多选模式下点卡片就是勾选/取消（1.2.12 口径 mappingBatchMode 的行为），
  // 要开详情先退出多选。
  if (batchMode.value) { togglePicked(item, !picked(item)); return }
  const request = ++inspectRequest
  missingRequest++; missingBusy.value = false; detailError.value = ''
  selected.value = withFiles(item); season.value = 1; seasonResult.value = null; seasonError.value = ''; completeError.value = ''; missingResult.value = null; missingError.value = ''
  embyItems.value = []; embyError.value = ''; if (item.unidentified) return; void loadEmby(item)
  // 列表卡片不含文件清单：详情单独按 id 取（1.2.12 口径 /api/emby/library/item）。
  try {
    const detail = await props.api(`/inventory/mapping/item?kind=${item.kind}&tmdb_id=${item.tmdb_id}`)
    if (request !== inspectRequest || !selected.value || workKey(selected.value) !== workKey(item)) return
    if (!detail?.item) throw new Error('作品详情未返回')
    selected.value = withFiles(detail.item)
  } catch (e) { if (request !== inspectRequest) return; detailError.value = `文件详情读取失败：${(e as Error).message}` }
  if (request !== inspectRequest || !selected.value || workKey(selected.value) !== workKey(item)) return
  if (item.kind === 'tv') { const cached = statusById.value[workKey(item)]?.data; if (cached) missingResult.value = cached; else await loadMissing() }
}
function closeDetails() { inspectRequest++; missingRequest++; embyGeneration++; selected.value = null; missingBusy.value = false; embyBusy.value = false }
async function loadEmby(item:any) {
  const request = ++embyGeneration; embyBusy.value = true; embyError.value = ''
  try { const result = await props.api(`/inventory/${item.kind}/${item.tmdb_id}/emby`); if (request === embyGeneration) embyItems.value = result.items || [] }
  catch (e) { if (request === embyGeneration) embyError.value = (e as Error).message }
  finally { if (request === embyGeneration) embyBusy.value = false }
}
async function loadMissing() {
  if (!selected.value || selected.value.kind !== 'tv') return
  const id = selected.value.tmdb_id, request = ++missingRequest
  missingBusy.value = true; missingError.value = ''; missingResult.value = null
  try {
    const data = await props.api(`/inventory/tv/${id}/missing`)
    if (request === missingRequest && selected.value?.tmdb_id === id) { missingResult.value = data; statusById.value[`tv:${id}`] = {data} }
  } catch (e) { if (request === missingRequest) missingError.value = (e as Error).message }
  finally { if (request === missingRequest) missingBusy.value = false }
}
async function toggleComplete() {
  if (!selected.value || selected.value.kind !== 'tv') return
  const item = selected.value
  completeBusy.value = true; completeError.value = ''
  try { const data = await props.api(`/inventory/tv/${item.tmdb_id}/manual-complete`, 'PUT', {complete:!item.manual_complete}); item.manual_complete = data.manual_complete; await loadPage() }
  catch (e) { completeError.value = (e as Error).message }
  finally { completeBusy.value = false }
}
async function loadSeason() {
  if (!selected.value || selected.value.kind !== 'tv') return
  const id = selected.value.tmdb_id, current = season.value
  seasonBusy.value = true; seasonError.value = ''; seasonResult.value = null
  try {
    const data = await props.api(`/inventory/tv/${id}/season/${current}`)
    if (selected.value?.tmdb_id === id && season.value === current) seasonResult.value = data
  } catch (e) { seasonError.value = (e as Error).message }
  finally { seasonBusy.value = false }
}
function searchResources() {
  if (selected.value) void router.push({name:'explore',query:{q:selected.value.title,resource_id:String(selected.value.tmdb_id),resource_kind:selected.value.kind}})
}
onMounted(async () => { window.addEventListener('resize', updatePageSize); await load(false); await loadHistory(); try { const config = await props.api('/inventory/config'); tie.value = config.tie; decision.value = config.decision || 'quality_first'; matchStrategy.value = config.match_strategy || 'title_year'; multiProtect.value = config.multi_season_protect || 'compare'; specialAction.value = config.special_action || 'compare'; exemptInput.value = (config.exempt_keywords || []).join('，'); singleQuality.value = config.single_quality; singleGaps.value = config.single_gaps; seasonRatio.value = Math.round((config.season_ratio ?? .9) * 100); quietMinutes.value = config.quiet_minutes ?? 15; scanScheduleEnabled.value = !!config.scan_schedule_enabled; scanIntervalHours.value = config.scan_interval_hours ?? 6; savedSettings.value = settingsKey(); const last = await props.api('/inventory/last-scan'); if (last.result) { preview.value = last.result; scanTime.value = Number(last.at || 0) } } catch (e) { configMessage.value = (e as Error).message } try { ruleInfo.value = await props.api('/upgrade/config') } catch (e) { ruleError.value = (e as Error).message } })
usePageActivity(() => { orphanVisible=true; void loadOrphanJobs() }, () => { orphanVisible=false; if(orphanTimer) clearTimeout(orphanTimer) })
onUnmounted(() => { window.removeEventListener('resize', updatePageSize); if(orphanTimer) clearTimeout(orphanTimer) })
</script>

<template>
 <section class="inventory-page">
  <header class="inventory-header"><div><h1>库存管理</h1><p class="muted">{{ tab === 'mapping' ? '查看片库与缺集情况' : tab === 'governance' ? '管理两库版本与重复内容' : '检查作品目录中的残留' }}</p></div><el-button :loading="busy" @click="load">刷新库存</el-button></header>
  <el-alert title="库存按本地两库中的 STRM 统计，未登记文件也会显示；网盘来源无法核实的文件不会直接删除源文件。清理前请核对计划清单。" type="info" :closable="false" show-icon />
  <el-alert v-if="!inventoryComplete" :title="`有 ${inventoryIssueCount} 条记录无法核实，统计可能不完整。`" type="warning" :closable="false" show-icon />
  <details v-if="!inventoryComplete && inventoryIssues.length"><summary>查看待核实记录</summary><p v-for="(issue, index) in inventoryIssues" :key="index" class="file-path">{{ side(issue.side) }} · {{ reason(issue.reason) }}：{{ issue.path || issue.id }}</p><el-button @click="openAuditFromMapping">前往核对并修复</el-button></details>
  <el-alert v-if="error" :title="error" type="error" :closable="false" />
  <p v-if="refreshAfterDeleteBusy" class="muted">清理已结束，正在后台刷新库存和计划记录…</p>
  <el-tabs v-model="tab"><el-tab-pane label="片库映射" name="mapping" /><el-tab-pane label="双库治理" name="governance" /><el-tab-pane label="目录残留清理" name="orphans" /></el-tabs>
  <template v-if="tab === 'mapping'">
   <section class="mapping-filters" aria-label="片库筛选">
    <div class="mapping-search-row">
     <el-input v-model="search" class="mapping-search" placeholder="搜索片名或 TMDB ID" clearable />
     <el-button :loading="statusBusy" @click="compareAll">对照全部剧集</el-button>
    </div>
    <div class="mapping-filter-row"><span class="mapping-filter-label">类型</span><el-radio-group v-model="kindFilter"><el-radio-button value="all">全部</el-radio-button><el-radio-button value="tv">剧集</el-radio-button><el-radio-button value="movie">电影</el-radio-button></el-radio-group><span class="mapping-filter-label">片库</span><el-radio-group v-model="scopeFilter"><el-radio-button value="all">全部</el-radio-button><el-radio-button value="cloud">网盘库</el-radio-button><el-radio-button value="share">分享库</el-radio-button><el-radio-button value="both">双库</el-radio-button></el-radio-group></div>
    <div class="mapping-filter-row"><span class="mapping-filter-label">目录分类</span><el-select v-model="categoryFilter" style="width:210px" placeholder="全部分类" clearable><el-option label="全部分类" value="all" /><el-option v-for="entry in allCategories" :key="entry.name" :label="`${entry.name}（${entry.count}）`" :value="entry.name" /></el-select><span class="mapping-filter-hint">{{ categorySource === 'emby' ? '分类来自 Emby 媒体库（按 TMDB 号匹配）' : 'Emby 未连接或未匹配，暂按目录关键词分类' }}</span></div>
    <div class="mapping-status-scroll"><el-radio-group v-model="statusFilter"><el-radio-button value="all">全部状态</el-radio-button><el-radio-button value="missing">缺集</el-radio-button><el-radio-button value="complete">完整</el-radio-button><el-radio-button value="extra">超集</el-radio-button><el-radio-button value="ongoing">在更</el-radio-button><el-radio-button value="pending">待对照</el-radio-button><el-radio-button value="unmatched">未识别</el-radio-button></el-radio-group></div>
   </section>
   <div class="mapping-legend"><span v-for="entry in STATUS_LEGEND" :key="entry.text" class="mapping-legend-item"><b>{{ entry.text }}</b>{{ entry.hint }}</span></div>
   <p class="mapping-note">库存扫描有个别问题的作品会另外标「库存待核实」，状态本身照常给。</p>
   <div class="mapping-result-bar"><div><strong>{{ pageBusy ? '加载中…' : mappingTotal + ' 部作品' }}</strong><span v-if="statusBusy" class="muted"> · 对照中 {{ statusProgress }}</span><span v-else-if="pageFallback" class="muted"> · 本地过滤</span></div><span v-if="!chosen.length && !batchMode" class="muted">点海报看详情 · <el-button link type="primary" size="small" @click="toggleBatchMode">多选清理</el-button></span></div>
   <div v-if="chosen.length || batchMode" class="mapping-selection-bar"><strong>{{ chosen.length ? '已选择 ' + chosen.length + ' 部' : '多选：横向滑过海报连续选择；从已选中的卡片起划是取消' }}</strong><div class="mapping-selection-actions"><el-button size="small" @click="selectPage">全选本页</el-button><el-button size="small" :disabled="!chosen.length" @click="unselectPage">取消本页</el-button><el-button size="small" :disabled="!chosen.length" @click="chosen = []">清空</el-button><el-button size="small" :loading="planBusy" :disabled="!chosen.length" @click="manualDelete(chosen,'cloud')">删除网盘库</el-button><el-button size="small" :loading="planBusy" :disabled="!chosen.length" @click="manualDelete(chosen,'share')">删除分享库</el-button><el-button size="small" type="danger" plain :loading="planBusy" :disabled="!chosen.length" @click="manualDelete(chosen,'both')">删除两库</el-button><el-button size="small" @click="toggleBatchMode">{{ batchMode ? '退出多选' : '多选清理' }}</el-button><span class="muted mapping-batch-limit">单次最多 100 部 / 10000 个文件</span></div></div>
   <el-skeleton v-if="busy && !visibleItems.length" :rows="6" animated />
   <el-empty v-else-if="!visibleItems.length" description="没有符合条件的作品" />
   <div v-else class="poster-wall" :class="{ 'swipe-select': batchMode }" @touchstart="wallTouchStart" @touchmove="wallTouchMove" @touchend="wallTouchEnd" @touchcancel="wallTouchEnd"><article v-for="item in visibleItems" :key="workKey(item)" class="poster-card" :class="{ 'batch-selected': picked(item) }" :data-work-key="workKey(item)" tabindex="0" role="button" :aria-label="`查看 ${item.title} 详情`" @click="inspect(item)" @keydown.enter.prevent="inspect(item)" @keydown.space.prevent="inspect(item)">
    <div class="poster-cover"><img loading="lazy" :src="item.unidentified ? `/api/v1/inventory/mapping/unidentified-poster/${item.id}` : `/api/v1/dashboard/tmdb-poster/${item.kind}/${item.tmdb_id}`" :alt="`${item.title} 海报`" @error="($event.target as HTMLImageElement).style.visibility='hidden'" /><span class="poster-type">{{ item.kind === 'tv' ? '剧集' : '电影' }}</span><span :class="['poster-status', cardStatus(item).tone]">{{ cardStatus(item).text }}</span><label v-if="!item.unidentified" class="poster-pick" @click.stop><input type="checkbox" :checked="picked(item)" :aria-label="`选择 ${item.title}`" @change="togglePicked(item, ($event.target as HTMLInputElement).checked)" /></label></div>
    <div class="poster-info"><strong :title="item.title">{{ item.title }}</strong><span>网盘 {{ item.cloud_files }} · 分享 {{ item.share_files }}</span></div>
   </article></div>
   <div v-if="totalPages > 1" class="inventory-pagination"><el-button :disabled="page <= 1" @click="page--">上一页</el-button><span>第 {{ page }} / {{ totalPages }} 页</span><el-button :disabled="page >= totalPages" @click="page++">下一页</el-button></div>
   <el-drawer :model-value="!!selected" :title="selected?.title || '作品详情'" size="min(94%, 600px)" destroy-on-close @close="closeDetails">
    <template v-if="selected">
     <template v-if="selected.unidentified"><el-alert title="未识别 TMDB，暂不支持缺集对照和删除；请先在整理侧修正作品身份。" type="warning" :closable="false" /><div v-for="lib in ['cloud','share']" :key="lib"><h4>{{ side(lib) }}（{{ selected[lib].length }}）</h4><p v-for="file in selected[lib]" :key="file.path" class="file-path">{{ file.path }}</p></div></template>
     <template v-else>
     <div class="detail-hero"><img :src="`/api/v1/dashboard/tmdb-poster/${selected.kind}/${selected.tmdb_id}`" alt="" /><div class="detail-hero-info"><h2>{{ selected.title }}</h2><span :class="['detail-badge', cardStatus(selected).tone]">{{ cardStatus(selected).text }}</span><p>TMDB {{ selected.tmdb_id }}<span v-if="selected.year"> · {{ selected.year }}</span></p><p class="muted">{{ cardStatus(selected).hint }}</p><div class="detail-library-counts"><span>网盘 <b>{{ selected.cloud_files }}</b> 个文件</span><span>分享 <b>{{ selected.share_files }}</b> 个文件</span></div></div></div>
     <el-alert v-if="detailError" :title="detailError" type="warning" :closable="false" />
     <div class="detail-actions"><el-button @click="searchResources">查找补集资源</el-button><el-button v-if="embyBusy" loading>查找 Emby</el-button><el-button v-if="embyItems.length" tag="a" :href="embyItems[0].url" target="_blank" rel="noopener noreferrer">在 Emby 中打开</el-button><el-button v-if="selected.kind === 'tv'" title="仅标记状态，缺集数据仍按 TMDB 实际显示" :loading="completeBusy" @click="toggleComplete">{{ selected.manual_complete ? '取消手动完结' : '标记手动完结' }}</el-button></div>
     <p v-if="embyError" class="muted detail-note">{{ embyError }}</p><p v-else-if="!embyBusy && !embyItems.length" class="muted detail-note">Emby 中尚未找到此作品。</p>
     <el-alert v-if="completeError" :title="completeError" type="error" :closable="false" />
     <section v-if="selected.kind === 'tv'" class="detail-section"><div class="detail-section-title"><div><strong>两库分集</strong><p class="muted">按 TMDB 已播集数对照</p></div><el-button :loading="missingBusy" @click="loadMissing">重新对照</el-button></div><el-alert v-if="missingError" :title="missingError" type="error" :closable="false" />
      <template v-if="missingResult"><el-alert v-if="!missingResult.tmdb_complete || !missingResult.inventory_complete" :title="`数据不完整${missingResult.failed_seasons.length ? '：TMDB 第 ' + missingResult.failed_seasons.join('、') + ' 季取不到，这些季暂不能判缺集' : ''}${missingResult.inventory_complete ? '' : '；库存扫描有记录未核实'}。下面只统计已核实的数据。`" type="warning" :closable="false" />
       <div class="detail-missing-summary"><span>网盘库缺 <b>{{ missingResult.cloud_missing }}</b> 集</span><span>分享库缺 <b>{{ missingResult.share_missing }}</b> 集</span></div>
       <div v-for="entry in missingResult.seasons" :key="entry.season" class="season-card"><div class="season-heading"><strong>{{ entry.season === 0 ? '特别篇（第 0 季）' : 'S' + String(entry.season).padStart(2,'0') }}</strong><span>{{ entry.official_known ? 'TMDB 已播 ' + entry.expected.length + ' 集' : '未取得该季 TMDB 对照' }}</span></div>
        <div v-for="lib in ['cloud','share']" :key="lib" class="library-episodes"><div class="library-season-heading"><strong>{{ side(lib) }}</strong><span>已有 {{ entry[lib].present.length }} 集</span><b :class="['season-status', librarySeasonStatus(entry, lib, missingResult.inventory_complete).tone]">{{ librarySeasonStatus(entry, lib, missingResult.inventory_complete).label }}</b></div><div v-if="librarySeasonStatus(entry, lib, missingResult.inventory_complete).percent !== null" class="season-progress-row"><div class="season-progress" role="progressbar" :aria-valuenow="librarySeasonStatus(entry, lib, missingResult.inventory_complete).percent ?? 0" aria-valuemin="0" aria-valuemax="100" :aria-label="`${side(lib)}已播集数入库进度`"><i :style="{width: librarySeasonStatus(entry, lib, missingResult.inventory_complete).percent + '%'}"></i></div><span>已播入库 {{ librarySeasonStatus(entry, lib, missingResult.inventory_complete).airedPresent }} / {{ entry.expected.length }}</span></div>
         <details class="episode-list"><summary>查看集号<span v-if="entry[lib].missing.length"> · 缺 {{ entry[lib].missing.length }}</span><span v-if="entry[lib].extra?.length"> · 超 {{ entry[lib].extra.length }}</span></summary><div class="episode-chips"><span v-for="ep in ordinaryEpisodes(entry[lib])" :key="'p'+ep" class="episode-present">E{{ String(ep).padStart(2,'0') }}</span><span v-for="ep in entry[lib].extra || []" :key="'x'+ep" class="episode-missing">E{{ String(ep).padStart(2,'0') }} 超</span><span v-for="ep in entry[lib].missing" :key="'m'+ep" class="episode-missing">E{{ String(ep).padStart(2,'0') }} 缺</span><span v-if="!entry[lib].present.length && !entry[lib].missing.length" class="muted">暂无数据</span></div></details>
         <details v-if="selected[lib].some((f:any) => f.season === entry.season)" class="episode-list"><summary>查看文件</summary><p v-for="file in selected[lib].filter((f:any) => f.season === entry.season)" :key="file.id" class="file-path">{{ file.name }}</p></details>
        </div><p v-if="entry.unknown_air_date.length" class="muted detail-note">播出日期未知：{{ entry.unknown_air_date.join('、') }}</p></div>
      </template>
     </section>
     <details v-if="selected.kind === 'tv'" class="detail-extra"><summary>单季查询</summary><div class="season-controls"><el-input-number v-model="season" :min="1" :max="999" :precision="0" /><el-button :loading="seasonBusy" @click="loadSeason">查询</el-button></div><el-alert v-if="seasonError" :title="seasonError" type="error" :closable="false" /><div v-if="seasonResult"><p v-if="!seasonResult.inventory_complete" class="muted">有记录未能核实，缺集统计可能不完整。</p><p>已播出 {{ seasonResult.expected.length }} 集</p><p>网盘库缺 {{ seasonResult.cloud.missing.length }} 集：{{ seasonResult.cloud.missing.join('、') || '无' }}</p><p>分享库缺 {{ seasonResult.share.missing.length }} 集：{{ seasonResult.share.missing.join('、') || '无' }}</p><p v-if="seasonResult.unknown_air_date.length" class="muted">播出日期未知：{{ seasonResult.unknown_air_date.join('、') }}；未计入缺集。</p></div></details>
     <el-alert v-if="selected.identity_conflicts?.length" title="同一 TMDB 编号对应不同作品目录，请先核对身份；暂不能删除。" type="warning" :closable="false" /><p v-for="v in selected.identity_conflicts || []" :key="v.path" class="file-path">{{ side(v.side) }} · {{ v.title }} · {{ v.path }}</p>
     <details class="detail-extra detail-danger"><summary>删除作品</summary><p class="muted">选择要清理的库，下一步会列出文件清单供核对。</p><div class="detail-actions"><el-button :loading="planBusy" @click="manualDelete([selected],'cloud')">删除网盘库</el-button><el-button :loading="planBusy" @click="manualDelete([selected],'share')">删除分享库</el-button><el-button type="danger" plain :loading="planBusy" @click="manualDelete([selected],'both')">删除两库</el-button></div></details>
     </template>
    </template>
   </el-drawer>
  </template>
  <template v-else-if="tab === 'governance'">
   <div class="gov-hero"><div><span class="gov-eyebrow">LIBRARY GOVERNANCE</span><h2>双库治理</h2><p>先清理单库问题，再按画质和集数比较两库。</p></div><el-button type="primary" size="large" :loading="previewBusy" @click="loadPreview">{{ preview ? '重新扫描' : '扫描双库' }}</el-button></div>
   <p class="muted">{{ scanTime ? `上次扫描：${new Date(scanTime * 1000).toLocaleString()}。计划有效期 2 小时，库存或规则变化后请重新扫描。` : '进入页面不会触发双库扫描；请手动扫描或启用定时扫描。' }}</p>
   <div class="gov-summary"><span>当前策略</span><strong>{{ decision === 'quality_first' ? '洗版优先' : decision === 'keep_local' ? '保留网盘库' : '保留分享库' }}</strong><i></i><span>平局保留{{ tie === 'cloud' ? '网盘库' : '分享库' }}</span><i></i><span>多季剧怎么处理{{ multiProtect === 'compare' ? '已开启' : '已关闭' }}</span><span v-if="settingsDirty" class="gov-unsaved">设置未保存</span></div>
   <details class="gov-panel gov-settings"><summary><span><strong>治理设置</strong><small>怎么选、怎么保护、什么时候不动</small></span><span class="gov-chevron">展开</span></summary><div class="gov-panel-body gov-settings-body">
   <section class="gov-group">
    <h4>怎么选</h4>
    <div class="gov-field"><span class="gov-field-label">两库都有的作品</span><el-radio-group v-model="decision" size="small"><el-radio-button value="quality_first">按画质洗版</el-radio-button><el-radio-button value="keep_local">留网盘库</el-radio-button><el-radio-button value="keep_share">留分享库</el-radio-button></el-radio-group><small class="gov-hint">按画质：逐维比较，先分出高下的一维定胜负；全部打平按下一项。</small></div>
    <div class="gov-field"><span class="gov-field-label">作品身份</span><el-radio-group v-model="matchStrategy" size="small"><el-radio-button value="title_year">剧名 + 年份</el-radio-button><el-radio-button value="tmdb">TMDB 优先</el-radio-button></el-radio-group><small class="gov-hint">TMDB 优先：按编号配对；同一编号对应多个剧名/年份时自动回退到剧名+年份（结果里会标出冲突）。</small></div>
    <div class="gov-field"><span class="gov-field-label">完全一样时</span><el-radio-group v-model="tie" size="small"><el-radio-button value="cloud">留网盘库</el-radio-button><el-radio-button value="share">留分享库</el-radio-button></el-radio-group></div>
    <div class="gov-field"><span class="gov-field-label">分享替换网盘的门槛</span><el-input-number v-model="seasonRatio" :min="50" :max="100" :step="5" :precision="0" size="small" controls-position="right" /><span class="gov-unit">%</span><small class="gov-hint">共同集里分享赢的比例达到这个数、且集数覆盖网盘全部集号，才整季替换。</small></div>
   </section>
   <section class="gov-group">
    <h4>多季与特别篇</h4>
    <div class="gov-field"><span class="gov-field-label">多季剧</span><el-radio-group v-model="multiProtect" size="small"><el-radio-button value="compare">防掏空</el-radio-button><el-radio-button value="off">逐季比较</el-radio-button></el-radio-group><small class="gov-hint">防掏空：季数相同或包含时保护较完整的合集；双方各有独有季时，共同季不因洗版而掏空任一库。逐季比较：各季独立裁决。</small></div>
    <div class="gov-field"><span class="gov-field-label">特别篇 S00</span><el-radio-group v-model="specialAction" size="small"><el-radio-button value="compare">比画质</el-radio-button><el-radio-button value="ignore">不动</el-radio-button><el-radio-button value="delete">两库都清</el-radio-button></el-radio-group></div>
    <el-alert v-if="specialAction === 'delete'" title="会把两库 S00 一起列入删除清单，执行前逐项核对。" type="warning" :closable="false" show-icon />
   </section>
   <section class="gov-group">
    <h4>范围与节奏</h4>
    <div class="gov-field"><span class="gov-field-label">新入库多久内不动</span><el-input-number v-model="quietMinutes" :min="0" :max="1440" :step="5" :precision="0" size="small" controls-position="right" /><span class="gov-unit">分钟</span><small class="gov-hint">设为 0 关闭。STRM 文件近期写入的作品暂不治理，避免把「还没入完」当成缺集。</small></div>
    <div class="gov-field"><span class="gov-field-label">定时扫描</span><el-switch v-model="scanScheduleEnabled" size="small" /><el-input-number v-model="scanIntervalHours" :disabled="!scanScheduleEnabled" :min="1" :max="168" :precision="0" size="small" controls-position="right" /><span class="gov-unit">小时</span><small class="gov-hint">只生成清理计划，不自动删除。</small></div>
    <div class="gov-field"><span class="gov-field-label">永不处理的片名</span><el-input v-model="exemptInput" placeholder="关键词用逗号分隔，命中的作品整部跳过" size="small" /><small class="gov-hint">标题或任一文件路径命中即豁免。</small></div>
    <div class="gov-field"><span class="gov-field-label">单库清理</span><el-switch v-model="singleQuality" active-text="多版本去重" size="small" /><el-switch v-model="singleGaps" active-text="缺集季" size="small" /><small class="gov-hint">多版本=同季同集多份留最优；缺集季=残次品（前序缺失/中间断层），逐侧清理。</small></div>
   </section>
   <section class="gov-group">
    <h4>判定规则</h4>
    <div class="rule-summary"><p v-if="ruleError" class="muted">规则读取失败：{{ ruleError }}</p><template v-else-if="ruleInfo"><div class="rule-list"><span v-for="rule in ruleInfo.config.rules.filter((r:any) => r.enabled && r.key !== 'size')" :key="rule.key">{{ ruleInfo.labels[rule.key] }}</span><span v-if="!ruleInfo.config.rules.some((r:any) => r.enabled && r.key !== 'size')">未启用可比较规则</span></div><p class="muted">依次比较，决胜后停止；STRM 文件大小不参与。无法识别的规格按规则中的未知档比较；全部打平时按平局设置。</p><el-button size="small" @click="router.push({name:'organize'})">去影视整理改洗版规则</el-button></template></div>
   </section>
   <div class="gov-save"><el-button type="primary" :loading="configBusy" @click="saveConfig">保存设置</el-button><span v-if="settingsDirty" class="gov-dirty">设置未保存</span><span class="muted">{{ configMessage }}</span></div>
  </div></details>
   <details ref="auditPanel" class="gov-panel gov-utility"><summary><span><strong>目录与台账核对</strong><small>查看扫描范围，核查失效记录</small></span><span class="gov-chevron">展开</span></summary><div class="gov-panel-body">
   <div><el-button :loading="auditBusy" @click="auditLibraries">核对库目录与台账</el-button><p class="muted">只读核对已配置的两库根目录；结果反映扫描时刻，不构成删除授权。</p></div>
   <el-alert v-if="auditError" :title="auditError" type="error" :closable="false" />
   <div v-if="audit"><el-alert :title="audit.clean ? '两库目录与台账数量一致' : '发现目录与台账不一致，请先核实'" :type="audit.clean ? 'success' : 'warning'" :closable="false" /><div v-for="(lib, key) in audit.libraries" :key="key"><p>{{ side(String(key)) }}（{{ lib.root }}）：台账 {{ lib.registered }}，磁盘 {{ lib.on_disk }}，缺失 {{ lib.missing_count }}，未登记 {{ lib.unregistered_count }}</p><details v-if="lib.missing_count || lib.unregistered_count"><summary>查看路径（各最多 200 条）</summary><p v-for="path in lib.missing" :key="path">台账有、磁盘无：{{ path }}</p><p v-for="path in lib.unregistered" :key="path">磁盘有、台账无：{{ path }}</p></details></div><p v-if="audit.duplicate_count || audit.outside_count">重复台账 {{ audit.duplicate_count }} 条，库根外台账 {{ audit.outside_count }} 条。</p></div>
   <div v-if="audit && !audit.clean"><el-button :loading="staleBusy" @click="checkStale">核查可修复的失效台账</el-button><p class="muted">只检查本地文件缺失、播放已撤销且没有输出归属的网盘记录。</p><el-alert v-if="staleError" :title="staleError" type="error" :closable="false" /><div v-if="stale.length"><p v-for="row in stale" :key="row.file_id">{{ row.relative_path }}</p><el-button type="warning" :loading="staleBusy" @click="clearStale">备份并修复上述记录</el-button></div></div><p v-if="staleBackup" class="muted">台账备份编号：{{ staleBackup }}</p>
   </div></details>
   <el-alert v-if="previewError" :title="previewError" type="error" :closable="false" />
   <template v-if="preview">
    <el-alert v-if="preview.unsupported?.length" :title="`有 ${preview.unsupported.length} 个文件来源不可核实（没登记或认不出作品）：这些文件不进清理计划，其他作品照常治理。`" type="info" :closable="false" show-icon />
    <el-alert v-if="preview.isolated_works || preview.isolated_records" :title="`已隔离 ${preview.isolated_works || 0} 部异常作品（涉及 ${preview.isolated_records || 0} 条目录或登记问题）；其余作品可正常生成计划。`" type="warning" :closable="false" show-icon />
    <div class="gov-metrics"><div><span>可清理</span><strong>{{ governanceActions.length }}</strong></div><div><span>需人工</span><strong>{{ preview.blocked.length }}</strong></div><div><span>已豁免</span><strong>{{ preview.exempt?.length || 0 }}</strong></div><div><span>两库配对</span><strong>{{ preview.pairs?.length || 0 }}</strong></div></div><p class="muted">按已保存的作品身份策略配对；点开一行可以看删哪些文件。这里不会动 Emby 的元数据。</p>
    <section class="gov-results">
    <el-alert v-if="preview.plan_error" :title="preview.plan_error" type="warning" :closable="false" /><p v-if="preview.plan_skipped_conflicts" class="muted">{{ preview.plan_skipped_conflicts }} 项冲突未纳入计划。</p>
    <div class="inventory-tools"><h3>治理清单（{{ governanceActions.length }}）</h3><el-button type="primary" :loading="planBusy" :disabled="!preview.scan_complete || !preview.plan || settingsDirty" @click="prepareAllGovernance">一键清理可处理项</el-button><el-button type="danger" :loading="planBusy" :disabled="!preview.scan_complete || settingsDirty || !governanceSelected.length || governanceSelected.length > 10000" @click="executeGovernanceSelection">执行选中项（{{ governanceSelected.length }}）</el-button><el-button @click="clearGovernanceSelection" :disabled="!governanceSelected.length">清空选择</el-button></div>
    <div class="inventory-tools"><el-radio-group v-model="governanceFilter"><el-radio-button value="all">全部</el-radio-button><el-radio-button value="cloud">网盘待清理</el-radio-button><el-radio-button value="share">分享待清理</el-radio-button><el-radio-button value="protected">需人工核实</el-radio-button><el-radio-button value="exempt">规则豁免</el-radio-button></el-radio-group><el-button v-if="['all','cloud','share'].includes(governanceFilter)" @click="selectAvailableActions">选择当前可处理项</el-button></div>
    <p class="muted">两侧各自缺集时，启用缺集季清理后可能同时列入计划；请核对每一侧的文件和删除后缺失的集数。</p>
    <el-alert v-if="selectionNote" :title="selectionNote" type="info" :closable="false" />
    <el-table v-if="['all','cloud','share'].includes(governanceFilter)" class="gov-desktop-list" :data="visibleGovernanceActions" :row-key="actionKey" empty-text="当前筛选没有待清理项目">
     <el-table-column type="expand"><template #default="s"><div class="governance-detail">
       <p><strong>{{ label(s.row) }}</strong> · {{ s.row.reason_label || (s.row.reason === 'quality' ? '单库多版本' : reason(s.row.reason)) }}<el-button link type="primary" size="small" class="gov-detail-link" @click="openWork(s.row)">查看作品详情</el-button></p>
       <p v-if="s.row.actionType === 'dual'">保留 {{ side(s.row.winner) }} · 将清理 {{ side(s.row.deleteSide) }} {{ s.row.fileCount }} 个文件</p>
       <p v-else>清理 {{ side(s.row.deleteSide) }} {{ s.row.fileCount }} 个文件</p>
      <p v-if="s.row.lost_episodes?.length" class="gov-loss">删后两库都不再有：{{ episodeList(s.row.lost_episodes) }}<span v-if="s.row.clears_show">（这部剧会被清空，需要重新入库）</span></p>
       <p v-if="s.row.present?.length">现有集号：{{ episodeList(s.row.present) }}；缺失集号：{{ episodeList(s.row.missing) }}</p>
       <p v-if="s.row.cloud_seasons || s.row.share_seasons">网盘季：{{ (s.row.cloud_seasons || []).join('、') || '无' }}；分享季：{{ (s.row.share_seasons || []).join('、') || '无' }}</p>
       <template v-if="s.row.sample"><p>画质对比依据<span v-if="s.row.sample.episode != null">（代表集 E{{ String(s.row.sample.episode).padStart(2,'0') }}）</span></p><p class="file-path">分享：{{ s.row.sample.share_name }}<br />网盘：{{ s.row.sample.cloud_name }}</p><p v-for="rule in s.row.rules || []" :key="rule.rule" class="comparison-rule">{{ rule.label }}：分享 {{ ruleValue(rule.new) }} / 网盘 {{ ruleValue(rule.old) }} · {{ rule.result === 0 ? '平局' : rule.result > 0 ? '分享胜出' : '网盘胜出' }}</p></template>
       <template v-if="s.row.actionType === 'dual'"><details v-for="lib in ['cloud','share']" :key="lib"><summary>{{ side(lib) }} · {{ lib === s.row.deleteSide ? '将删除' : '保留' }} · {{ (s.row[lib + '_files'] || []).length }} 个文件</summary><p v-for="file in s.row[lib + '_files'] || []" :key="file.id" class="file-path">{{ file.path }}</p></details></template>
       <details v-else><summary>查看文件（{{ (s.row.files || []).length }}）</summary><p v-for="(file, index) in s.row.files || []" :key="file.id" class="file-path">{{ file.path }} · {{ s.row.reason === 'quality' && s.row.winner === index ? '保留' : '将删除' }}</p></details>
       <p v-if="actionDisabledReason(s.row)" class="muted">{{ actionDisabledReason(s.row) }}</p>
     </div></template></el-table-column>
     <el-table-column label="选择" width="62"><template #default="s"><el-checkbox :model-value="selectedAction(s.row)" :disabled="!!actionDisabledReason(s.row)" :aria-label="'选择 ' + label(s.row)" @change="toggleGovernanceAction(s.row, Boolean($event))" /></template></el-table-column>
     <el-table-column label="作品" min-width="180"><template #default="s"><el-tag v-if="s.row.clears_show" type="danger" size="small">删后清空</el-tag><el-tag v-if="s.row.unregistered?.cloud" type="warning" size="small">网盘源未登记 {{ s.row.unregistered.cloud }}</el-tag> {{ label(s.row) }}<p v-if="actionDisabledReason(s.row)" class="muted selection-reason">{{ actionDisabledReason(s.row) }}</p></template></el-table-column><el-table-column label="处理原因" min-width="150"><template #default="s">{{ s.row.reason_label || (s.row.reason === 'quality' ? '单库多版本' : reason(s.row.reason)) }}</template></el-table-column><el-table-column label="清理库" width="100"><template #default="s">{{ side(s.row.deleteSide) }}</template></el-table-column><el-table-column prop="fileCount" label="文件" width="70" />
    </el-table>
    <div v-if="['all','cloud','share'].includes(governanceFilter)" class="gov-mobile-list">
     <p v-if="!visibleGovernanceActions.length" class="muted">当前筛选没有待清理项目</p>
     <article v-for="row in visibleGovernanceActions" :key="actionKey(row)" class="gov-action-card">
      <div class="gov-action-heading"><el-checkbox :model-value="selectedAction(row)" :disabled="!!actionDisabledReason(row)" :aria-label="'选择 ' + label(row)" @change="toggleGovernanceAction(row, Boolean($event))" /><div><strong>{{ label(row) }}</strong><p><el-tag v-if="row.unregistered?.cloud" type="warning" size="small">网盘源未登记 {{ row.unregistered.cloud }}</el-tag>{{ row.reason_label || (row.reason === 'quality' ? '单库多版本' : reason(row.reason)) }}</p></div><span class="gov-action-badge">{{ side(row.deleteSide) }} · {{ row.fileCount }} 个</span></div>
      <p v-if="actionDisabledReason(row)" class="gov-action-note">{{ actionDisabledReason(row) }}</p>
      <p v-if="row.single_side" class="gov-loss">另一侧没有这一季：删掉后这一季在两库都不存在。</p>
      <p v-if="row.lost_episodes?.length" class="gov-loss">删后两库都不再有：{{ episodeList(row.lost_episodes) }}<span v-if="row.clears_show">（这部剧会被清空）</span></p>
      <details class="gov-action-detail"><summary>查看判断依据与文件</summary>
       <p v-if="row.actionType === 'dual'">保留 {{ side(row.winner) }} · 清理 {{ side(row.deleteSide) }}</p>
       <p v-if="row.present?.length">现有：{{ episodeList(row.present) }}；缺失：{{ episodeList(row.missing) }}</p>
       <p v-if="row.cloud_seasons || row.share_seasons">网盘季：{{ (row.cloud_seasons || []).join('、') || '无' }}；分享季：{{ (row.share_seasons || []).join('、') || '无' }}</p>
       <p v-for="(why, index) in row.reasons || []" :key="index">{{ why }}</p>
       <template v-if="row.sample"><p v-if="row.sample.episode != null">代表集 E{{ String(row.sample.episode).padStart(2,'0') }}</p><p class="file-path">分享：{{ row.sample.share_name }}<br />网盘：{{ row.sample.cloud_name }}</p><p v-for="rule in row.rules || []" :key="rule.rule" class="comparison-rule">{{ rule.label }}：分享 {{ ruleValue(rule.new) }} / 网盘 {{ ruleValue(rule.old) }} · {{ rule.result === 0 ? '平局' : rule.result > 0 ? '分享胜出' : '网盘胜出' }}</p></template>
       <details v-if="row.comparisons?.length"><summary>查看逐集比较（{{ row.comparisons.length }}）</summary><div v-for="comparison in row.comparisons" :key="comparison.episode"><p>第 {{ comparison.episode }} 集：{{ comparison.tie ? '平局，按设置保留' : '胜出' }} {{ side(comparison.winner) }}</p><p v-for="rule in comparison.rules" :key="rule.rule" class="comparison-rule">{{ rule.label }}：分享 {{ ruleValue(rule.new) }} / 网盘 {{ ruleValue(rule.old) }} · {{ rule.result === 0 ? '平局' : rule.result > 0 ? '分享胜出' : '网盘胜出' }}</p></div></details>
       <template v-if="row.actionType === 'dual'"><details v-for="lib in ['cloud','share']" :key="lib"><summary>{{ side(lib) }} · {{ lib === row.deleteSide ? '将删除' : '保留' }} · {{ (row[lib + '_files'] || []).length }} 个文件</summary><p v-for="file in row[lib + '_files'] || []" :key="file.id" class="file-path">{{ file.path }}</p></details></template>
       <details v-else><summary>查看文件（{{ (row.files || []).length }}）</summary><p v-for="(file, index) in row.files || []" :key="file.id" class="file-path">{{ file.path }} · {{ row.reason === 'quality' && row.winner === index ? '保留' : '将删除' }}</p></details>
      </details>
     </article>
    </div>
    <template v-if="governanceFilter === 'exempt' || (governanceFilter === 'all' && preview.exempt?.length)"><h3>规则豁免（{{ preview.exempt?.length || 0 }}）</h3><el-empty v-if="!preview.exempt?.length" description="没有规则豁免项目" :image-size="60" /><el-collapse v-else><el-collapse-item v-for="(row, index) in preview.exempt" :key="index" :title="label(row)" :name="index"><p>命中关键词：{{ (row.keywords || []).join('、') || '无' }}</p><p v-if="row.member_count">涉及 {{ row.member_count }} 项</p><p v-for="(member, i) in row.members || []" :key="i">{{ member.season == null ? member : '第 ' + member.season + ' 季' }}</p><details v-for="lib in ['cloud','share']" :key="lib"><summary>{{ side(lib) }} · {{ (row[lib + '_files'] || []).length }} 个文件</summary><p v-for="file in row[lib + '_files'] || []" :key="file.id" class="file-path">{{ file.path }}</p></details></el-collapse-item></el-collapse></template>
    <details v-if="governanceFilter === 'all'" class="gov-advanced"><summary>查看配对、单库及双库原始明细</summary>
    <h3>两库配对明细（{{ preview.pairs?.length || 0 }}）</h3>
    <el-empty v-if="!preview.pairs?.length" description="没有找到同一作品或同一季的双库记录" :image-size="60" />
    <el-collapse v-else><el-collapse-item v-for="(pair, index) in preview.pairs" :key="index" :title="`${label(pair)} · 网盘 ${pair.cloud_files.length} 文件 / 分享 ${pair.share_files.length} 文件`" :name="index"><div v-if="pair.kind === 'tv'" class="governance-evidence"><p>网盘集号：{{ episodeList(pair.cloud_episodes) }}</p><p>分享集号：{{ episodeList(pair.share_episodes) }}</p><p>共同集：{{ episodeList(pair.common_episodes) }}</p><p>仅网盘：{{ episodeList(pair.cloud_only) }}；仅分享：{{ episodeList(pair.share_only) }}</p></div><details v-for="lib in ['cloud','share']" :key="lib"><summary>{{ side(lib) }}文件（{{ pair[lib + '_files'].length }}）</summary><p v-for="file in pair[lib + '_files']" :key="file.id" class="file-path">{{ file.path }}</p></details></el-collapse-item></el-collapse>
    </details>
    <template v-if="governanceFilter === 'protected' || (governanceFilter === 'all' && preview.blocked.length)">
    <h3>需人工核实（{{ preview.blocked.length }}）</h3><p class="muted">这些项目不会自动清理：要么两库凑不齐集数（删任一侧都会丢集），要么还在静默期或存在待核实记录。</p>
    <el-empty v-if="!preview.blocked.length" description="没有待核实项目" :image-size="60" />
    <el-collapse v-else><el-collapse-item v-for="(row, index) in preview.blocked" :key="index" :title="`${label(row)} · ${reason(row.reason)}`" :name="index"><p v-if="row.detail">{{ row.detail }}</p><p v-if="row.missing_on_keeper?.length">保留方缺少以下集数，不能删除另一方：{{ episodeList(row.missing_on_keeper) }}</p><p v-if="row.quiet_minutes">入库静默期 {{ row.quiet_minutes }} 分钟<template v-if="row.ingested_at">（最近入库 {{ new Date(row.ingested_at * 1000).toLocaleTimeString() }}<template v-if="row.quiet_remaining">，还剩 {{ row.quiet_remaining }} 分钟</template>）</template></p><template v-if="row.cloud_episodes"><p>网盘集号：{{ episodeList(row.cloud_episodes) }}</p><p>分享集号：{{ episodeList(row.share_episodes) }}</p><p>共同集：{{ episodeList(row.common_episodes) }}；仅网盘：{{ episodeList(row.cloud_only) }}；仅分享：{{ episodeList(row.share_only) }}</p></template><details v-for="lib in ['cloud','share']" v-show="row[lib + '_files']?.length" :key="lib"><summary>{{ side(lib) }}文件（{{ row[lib + '_files']?.length }}）</summary><p v-for="file in row[lib + '_files']" :key="file.id" class="file-path">{{ file.path }}</p></details><p v-for="file in row.files || []" :key="file.id" class="file-path">{{ file.path }}</p></el-collapse-item></el-collapse>
    </template>
    </section>
   </template>
   <details class="gov-panel gov-history"><summary><span><strong>最近清理计划</strong><small>查看执行记录与进度</small></span><span class="gov-chevron">展开</span></summary><div class="gov-panel-body"><div class="inventory-tools"><el-button @click="loadHistory">刷新状态</el-button><span class="muted">执行中断的计划不会自动重试。</span></div><el-alert v-if="historyError" :title="historyError" type="error" :closable="false" /><el-table :data="history" empty-text="暂无清理计划"><el-table-column prop="mode_label" label="类型" width="110" /><el-table-column label="创建时间" min-width="160"><template #default="s">{{ new Date(s.row.created * 1000).toLocaleString() }}</template></el-table-column><el-table-column prop="state" label="状态" width="110" /><el-table-column label="进度" width="100"><template #default="s">{{ s.row.done }}/{{ s.row.count }}</template></el-table-column><el-table-column prop="error" label="说明" min-width="160" /></el-table></div></details>
  </template>
  <template v-else>
   <el-alert title="扫描本地两库和 115 归档目录。网盘侧同一作品可联动清理本地残留及 115 字幕、元数据和空目录；分享侧只清理本地。只处理带 TMDB 标识且没有视频或未知文件的目录，115 文件进入回收站。" type="info" :closable="false" show-icon />
   <div class="inventory-tools"><el-button type="primary" :loading="orphanBusy" @click="scanOrphans">扫描本地及 115 残留</el-button><el-button type="danger" :loading="orphanBusy" :disabled="!orphanSelected.length || orphanSelected.length > 100" @click="cleanOrphans">清理选中项</el-button><span class="muted">每次最多清理 100 项</span></div>
   <el-alert v-if="orphanError" :title="orphanError" type="error" :closable="false" /><el-alert v-if="cloudOrphanError" :title="cloudOrphanError" type="warning" :closable="false" />
   <template v-if="orphanResult"><p>发现 {{ orphanResult.items.length }} 个可清理残留项</p>
   <details v-if="orphanResult.attention_count || orphanResult.cloudAttentionCount" class="orphan-attention">
    <summary>有 {{ (orphanResult.attention_count || 0) + (orphanResult.cloudAttentionCount || 0) }} 项需要核对 <span>展开查看</span></summary>
    <p class="muted">以下目录暂不清理，请核对原因；含正常视频或 STRM 的目录已自动保留，不在此显示。</p>
    <p v-for="(row,i) in orphanResult.attention_items" :key="'local'+i" class="file-path">{{ side(row.side) }} · {{ row.path }}<br>{{ skipReason(row.reason) }}<span v-if="row.detail">：{{row.detail}}</span></p>
    <p v-for="(row,i) in orphanResult.cloudAttentionItems" :key="'cloud'+i" class="file-path">115 网盘 · {{row.path || row.id}}<br>{{row.reason}}</p>
    <p v-if="(orphanResult.attention_count || 0) > (orphanResult.attention_items?.length || 0) || orphanResult.cloudAttentionCount > orphanResult.cloudAttentionItems.length" class="muted">仅显示部分条目。</p>
   </details><el-table :data="orphanResult.items" @selection-change="orphanSelected = $event"><el-table-column type="expand"><template #default="s"><div v-if="s.row.linkedCloud"><strong>115 网盘待回收附属文件</strong><p v-for="(name,i) in s.row.linkedCloud.file_names" :key="i" class="file-path">{{name}}</p><p v-if="s.row.linkedCloud.files>s.row.linkedCloud.file_names.length" class="muted">仅显示前 {{s.row.linkedCloud.file_names.length}} 个文件；本项共 {{s.row.linkedCloud.files}} 个。</p><p v-if="!s.row.linkedCloud.files" class="muted">目录已空，无附属文件。</p></div><p v-else class="muted">只清理本地残留。</p></template></el-table-column><el-table-column type="selection" width="45" /><el-table-column label="库" width="90"><template #default="s">{{ side(s.row.side) }}<span v-if="s.row.linkedCloud&&s.row.side==='cloud'"> + 115</span></template></el-table-column><el-table-column label="类型" width="100"><template #default="s">{{ s.row.kind === 'file' ? '附属文件' : '作品目录' }}</template></el-table-column><el-table-column prop="path" label="路径" min-width="240" /><el-table-column prop="files" label="附属文件" width="100" /><el-table-column prop="dirs" label="目录" width="70" /></el-table></template>
   <div v-if="orphanJob && ['queued','running'].includes(orphanJob.state)" class="orphan-progress">
    <strong>正在清理：{{ orphanJob.done }} / {{ orphanJob.count }} 项</strong>
    <el-progress :percentage="Math.round(orphanJob.done * 100 / orphanJob.count)" />
    <p>{{ orphanJob.current?.phase || '等待执行' }}<span v-if="orphanJob.index"> · 第 {{ orphanJob.index }} 项</span></p>
    <p class="file-path">{{ orphanJob.current?.path }}</p>
    <p v-if="orphanJob.current?.files_total != null" class="muted">文件 {{orphanJob.current.files_done}} / {{orphanJob.current.files_total}} · 目录 {{orphanJob.current.dirs_done}} / {{orphanJob.current.dirs_total}}</p>
    <p v-if="orphanJob.current?.current" class="file-path">正在核对：{{orphanJob.current.current}}</p>
    <small class="muted">115 文件需要逐个核对，可能耗时较长；离开页面后仍会继续，请勿重复清理。</small>
   </div>
   <details class="orphan-history" :open="!!orphanJobs.length"><summary><strong>残留清理记录</strong><span>最近 {{orphanJobs.length}} 次 · 展开或收起</span></summary>
    <p v-if="!orphanJobs.length" class="muted">暂无记录；升级前的清理结果不会自动补录。</p>
    <details v-for="job in orphanJobs" :key="job.id" class="orphan-job">
     <summary><span><strong>{{new Date(job.created*1000).toLocaleString()}}</strong><small>{{jobState(job.state)}} · 已处理 {{job.done}} / {{job.count}} 项</small></span><span>详情</span></summary>
     <p v-if="job.error" class="orphan-error">{{job.error}}</p>
     <details v-if="job.state === 'interrupted' && job.current?.entries?.length"><summary>中断前已确认回收（{{job.current.entries.length}}）</summary><p v-for="(entry,j) in job.current.entries" :key="j" class="file-path">{{entry.kind}} · {{entry.path}}</p></details>
     <div v-for="(row,i) in job.results" :key="i" class="orphan-record">
      <div><el-tag size="small" :type="row.errors?.length?'warning':'success'">{{row.errors?.length?'未完成':'已完成'}}</el-tag> <strong>{{row.location}}</strong> · 文件 {{row.files_removed}} · 目录 {{row.dirs_removed}}</div>
      <p class="file-path">{{row.path}}</p><p v-for="err in row.errors" :key="err" class="orphan-error">{{err}}</p>
      <details v-if="row.entries?.length"><summary>查看已回收的文件和目录（{{row.entries.length}}）</summary><p v-for="(entry,j) in row.entries" :key="j" class="file-path">{{entry.kind}} · {{entry.path}}</p></details>
     </div>
    </details>
   </details>
  </template>
  <el-alert v-if="planError && !planOpen" :title="planError" type="error" :closable="false" />
  <el-dialog v-model="planOpen" :title="planTitle" width="min(90%, 760px)" :close-on-click-modal="false" :close-on-press-escape="!planBusy" :show-close="!planBusy">
   <template v-if="!plan"><el-alert v-if="planError" :title="planError" type="error" :closable="false" /><p v-else class="muted">正在核对库存、文件来源与规则，生成清理清单…</p></template>
   <template v-else><el-alert :title="`${PLAN_LABELS[planMode] || '清理'}${planScope ? '（' + planScope + '）' : ''}：${planMode === 'manual' ? '只清理所选作品的文件，网盘进 115 回收站，分享只删本地 STRM。' : '按治理判定清理被淘汰的一侧。'}扫描计划 2 小时内有效，其他计划 15 分钟内有效；库存变化会停止执行。`" type="warning" :closable="false" /><p>共 {{ plan.actions.length }} 个文件</p><el-table :data="plan.actions" max-height="350"><el-table-column label="库" width="100"><template #default="s">{{ side(s.row.side) }}</template></el-table-column><el-table-column prop="path" label="文件路径" /></el-table><el-alert v-if="planError" :title="planError" type="error" :closable="false" /><div v-if="execution"><el-progress :percentage="execution.count ? Math.round(execution.progress.length * 100 / execution.count) : 0" :status="execution.state === 'failed' || planError ? 'exception' : execution.state === 'completed' ? 'success' : undefined" /><p>状态：{{ planError ? '待核实' : execution.state }} · 已处理 {{ execution.progress.length }}/{{ execution.count }}<span v-if="execution.error"> · {{ execution.error }}</span></p><p v-if="!planError && ['starting','executing'].includes(execution.state) && !execution.current" class="muted">正在核对执行前库存，请稍候…</p><p v-if="!planError && ['starting','executing'].includes(execution.state) && execution.current" class="file-path">正在处理并核对 {{ side(execution.current.side) }}：{{ execution.current.path }}</p></div></template>
   <template #footer><el-button :disabled="planBusy" @click="planOpen = false">关闭</el-button><el-button v-if="plan" type="danger" :loading="planBusy" :disabled="!!execution" @click="executePlan">确认并执行</el-button></template>
  </el-dialog>
 </section>
</template>

<style scoped>
.orphan-attention{margin:12px 0;padding:12px;border:1px solid var(--el-border-color-lighter);border-radius:9px}.orphan-attention summary{cursor:pointer}.orphan-attention summary span{float:right;color:var(--el-text-color-secondary);font-size:12px}
.orphan-progress,.orphan-history{margin-top:18px;padding:16px;border:1px solid var(--el-border-color-lighter);border-radius:12px;background:var(--el-fill-color-blank)}
.orphan-progress p{margin:10px 0}.orphan-progress .el-progress{margin-top:12px}.orphan-history>summary,.orphan-job>summary{display:flex;justify-content:space-between;gap:12px;cursor:pointer;align-items:center}.orphan-history>summary span,.orphan-job summary span:last-child{color:var(--el-text-color-secondary);font-size:12px}.orphan-job{margin-top:12px;padding:12px;border:1px solid var(--el-border-color-lighter);border-radius:9px}.orphan-job summary strong,.orphan-job summary small{display:block}.orphan-job summary small{margin-top:5px;color:var(--el-text-color-secondary)}.orphan-record{padding:12px 0;border-top:1px solid var(--el-border-color-lighter);margin-top:12px;font-size:13px}.orphan-record p{margin:7px 0}.orphan-error{color:var(--el-color-danger);font-size:12px;word-break:break-word}
.mapping-batch-limit{font-size:12px}

.poster-wall.swipe-select{touch-action:pan-y}
.poster-card.batch-selected{outline:2px solid var(--el-color-primary);outline-offset:-2px}
.poster-card.batch-selected .poster-cover::after{content:'✓';position:absolute;right:8px;top:8px;width:22px;height:22px;border-radius:11px;background:var(--el-color-primary);color:#fff;font-size:13px;display:flex;align-items:center;justify-content:center}
.poster-cover{position:relative}

.mapping-filter-hint{font-size:12px;color:var(--el-text-color-secondary)}

.mapping-legend{display:flex;flex-wrap:wrap;gap:6px 18px;margin:4px 0 2px;font-size:12px;color:var(--el-text-color-secondary)}
.mapping-legend-item b{color:var(--el-text-color-primary);margin-right:6px;font-weight:600}
.gov-detail-link{margin-left:10px}


.gov-settings-body{display:grid;gap:18px}
.gov-group{display:grid;gap:10px}
.gov-group h4{display:flex;align-items:center;gap:10px;margin:0;font-size:13px;font-weight:600;color:var(--el-text-color-primary)}
.gov-group h4::after{content:'';flex:1;height:1px;background:var(--el-border-color-lighter)}
.gov-field{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:10px 12px;border:1px solid var(--el-border-color-lighter);border-radius:10px}
.gov-field-label{min-width:136px;font-size:13px;color:var(--el-text-color-regular)}
.gov-field .gov-hint{flex-basis:100%;font-size:12px;line-height:1.5;color:var(--el-text-color-secondary)}
.gov-unit{font-size:12px;color:var(--el-text-color-secondary)}
.gov-save{position:sticky;bottom:0;display:flex;align-items:center;gap:12px;padding:12px 0 2px;background:linear-gradient(to top,var(--el-bg-color) 70%,transparent)}
.gov-dirty{font-size:12px;color:var(--el-color-warning)}
@media(max-width:639px){.gov-field-label{min-width:0;flex-basis:100%}}

.gov-loss{color:#c45656;font-weight:600}
.inventory-page{display:grid;gap:16px}.inventory-header{display:flex;align-items:center;justify-content:space-between;gap:16px}.inventory-header h1{margin:0}.inventory-header p{margin:6px 0 0}.inventory-tools,.season-controls{display:flex;align-items:center;flex-wrap:wrap;gap:12px}.inventory-tools .el-input{max-width:360px}.season-controls{margin:20px 0}.poster-wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}.poster-card{min-width:0;cursor:pointer;border:1px solid var(--line);background:var(--surface);border-radius:9px;overflow:hidden;outline-offset:4px;box-shadow:var(--panel-shadow)}.poster-card:focus-visible{outline:2px solid var(--el-color-primary)}.poster-cover{position:relative;aspect-ratio:2/3;overflow:hidden;background:var(--el-fill-color-light)}.poster-cover img{width:100%;height:100%;object-fit:cover;transition:transform .2s}.poster-card:hover img{transform:scale(1.035)}.poster-type,.poster-status,.poster-pick{position:absolute;z-index:1;border-radius:6px;padding:4px 7px;color:white;background:#182534dd;font-size:10px}.poster-type{left:8px;top:8px}.poster-status{left:8px;bottom:8px}.poster-status.ok,.detail-badge.ok{background:#15835ce8}.poster-status.missing,.detail-badge.missing{background:#c34740ee}.poster-status.ongoing,.detail-badge.ongoing{background:#326cd6ed}.poster-status.extra,.detail-badge.extra{background:#a75f20ed}.poster-status.unknown,.detail-badge.unknown{background:#8b641ee8}.poster-pick{right:8px;top:8px;display:flex;align-items:center;justify-content:center;min-width:38px;min-height:38px;padding:8px;border:1px solid rgba(255,255,255,.65);border-radius:8px;background:rgba(15,23,42,.85);cursor:pointer}.poster-pick input{width:18px;height:18px;margin:0;accent-color:var(--el-color-primary);cursor:pointer}.poster-info{display:grid;gap:4px;padding:10px}.poster-info strong{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:14px}.poster-info span{font-size:11px;color:var(--el-text-color-secondary)}.inventory-pagination{display:flex;justify-content:center;align-items:center;gap:14px}.detail-hero{display:flex;gap:18px;align-items:flex-start;margin-bottom:20px}.detail-hero img{width:116px;aspect-ratio:2/3;object-fit:cover;border-radius:10px;background:var(--el-fill-color-light)}.detail-hero h2{margin:0 0 10px}.detail-badge{display:inline-block;padding:4px 8px;border-radius:6px;background:#566476;color:white}.season-card{border:1px solid var(--el-border-color-lighter);border-radius:10px;padding:14px;margin:12px 0}.season-heading,.library-episodes>div:first-child{display:flex;justify-content:space-between;gap:12px}.season-heading{padding-bottom:10px;border-bottom:1px solid var(--el-border-color-lighter)}.season-heading span,.library-episodes span{font-size:12px;color:var(--el-text-color-secondary)}.library-episodes{padding:12px 0 0}.episode-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.episode-chips span{padding:4px 7px;border-radius:5px}.episode-present{background:var(--el-color-success-light-9);color:var(--el-color-success)!important}.episode-missing{background:var(--el-color-danger-light-9);color:var(--el-color-danger)!important}.file-path{font-size:12px;word-break:break-all;color:var(--el-text-color-secondary)}@media(max-width:639px){.poster-wall{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.poster-info{padding:8px 7px}.poster-info strong{font-size:12px}.detail-hero img{width:88px}}
.library-season-heading{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.library-season-heading strong{margin-right:auto}.season-status{border-radius:5px;padding:3px 7px;font-size:12px;background:var(--el-fill-color-light)}.season-status.complete{color:var(--el-color-success)}.season-status.missing{color:var(--el-color-danger)}.season-status.extra,.season-status.unknown{color:var(--el-color-warning)}.season-progress-row{display:flex;align-items:center;gap:10px;margin-top:8px}.season-progress{height:6px;flex:1;border-radius:6px;background:var(--el-fill-color);overflow:hidden}.season-progress i{display:block;height:100%;border-radius:inherit;background:var(--el-color-success)}.season-progress-row span{font-size:12px;color:var(--el-text-color-secondary);white-space:nowrap}
</style>
<style scoped>
.rule-summary{padding:14px;border:1px solid var(--el-border-color-lighter);border-radius:9px;background:var(--el-fill-color-blank)}.rule-summary p{font-size:12px;margin:8px 0}.rule-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}.rule-list span{padding:4px 8px;border-radius:6px;background:var(--el-color-primary-light-9);font-size:12px;color:var(--el-color-primary)}.governance-evidence{display:grid;gap:8px}.governance-evidence p{margin:3px 0;font-size:12px;overflow-wrap:anywhere}.comparison-rule{font-size:12px;color:var(--el-text-color-secondary)}
.governance-detail{padding:8px 20px;overflow-wrap:anywhere}.governance-detail p{margin:8px 0}.governance-detail details{margin:10px 0}.selection-reason{font-size:11px;margin:3px 0;line-height:1.4}
.gov-hero{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:24px 26px;border:1px solid var(--el-border-color-lighter);border-radius:16px;background:linear-gradient(125deg,var(--el-color-primary-light-9),var(--el-bg-color) 70%)}
.gov-hero h2{margin:5px 0 6px;font-size:23px;letter-spacing:-.02em}.gov-hero p{margin:0;color:var(--el-text-color-secondary);font-size:13px;line-height:1.6}.gov-eyebrow{font-size:10px;letter-spacing:.16em;font-weight:700;color:var(--el-color-primary)}
.gov-summary{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:0 4px;color:var(--el-text-color-secondary);font-size:12px}.gov-summary strong{color:var(--el-text-color-primary);font-weight:600}.gov-summary i{width:3px;height:3px;border-radius:50%;background:var(--el-text-color-placeholder)}.gov-unsaved{color:var(--el-color-warning)}
.gov-panel,.gov-results{border:1px solid var(--el-border-color-lighter);border-radius:14px;background:var(--el-bg-color);overflow:hidden}.gov-panel>summary{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 18px;cursor:pointer;list-style:none}.gov-panel>summary::-webkit-details-marker{display:none}.gov-panel>summary span:first-child{display:flex;align-items:baseline;flex-wrap:wrap;gap:10px}.gov-panel>summary strong{font-size:14px}.gov-panel>summary small,.gov-chevron{font-size:12px;color:var(--el-text-color-secondary)}.gov-panel[open] .gov-chevron{color:var(--el-color-primary)}.gov-panel-body{display:grid;gap:14px;padding:16px 18px 20px;border-top:1px solid var(--el-border-color-lighter)}
.gov-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.gov-metrics>div{display:grid;gap:6px;padding:15px 17px;border:1px solid var(--el-border-color-lighter);border-radius:12px;background:var(--el-bg-color)}.gov-metrics span{color:var(--el-text-color-secondary);font-size:12px}.gov-metrics strong{font-size:23px;font-weight:650;line-height:1.15}.gov-results{display:grid;gap:14px;padding:20px}.gov-results h3{margin:0;font-size:16px}.gov-results>.muted{margin:0;font-size:12px;line-height:1.6}.gov-advanced{padding:14px 0;border-top:1px solid var(--el-border-color-lighter)}.gov-advanced>summary{cursor:pointer;color:var(--el-color-primary);font-size:13px}.gov-advanced h3{margin:18px 0 10px}.gov-history{margin-top:2px}.gov-mobile-list{display:none}
@media(max-width:639px){.gov-hero{padding:18px;align-items:flex-start;gap:10px}.gov-hero h2{font-size:20px}.gov-hero .el-button{flex-shrink:0}.gov-metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.gov-metrics>div{padding:12px 14px}.gov-metrics strong{font-size:20px}.gov-results{padding:14px;gap:12px}.gov-panel>summary{padding:14px}.gov-panel-body{padding:14px}.gov-summary{gap:7px}.gov-desktop-list{display:none}.gov-mobile-list{display:grid;gap:9px}.gov-action-card{padding:12px;border:1px solid var(--el-border-color-lighter);border-radius:10px;background:var(--el-fill-color-blank)}.gov-action-heading{display:flex;align-items:flex-start;gap:8px}.gov-action-heading>div{flex:1;min-width:0}.gov-action-heading strong{font-size:13px;line-height:1.5;overflow-wrap:anywhere}.gov-action-heading p{font-size:12px;color:var(--el-text-color-secondary);margin:4px 0 0}.gov-action-badge{font-size:11px;white-space:nowrap;color:var(--el-color-primary);background:var(--el-color-primary-light-9);border-radius:6px;padding:4px 6px}.gov-action-note{font-size:11px;line-height:1.5;color:var(--el-color-warning);margin:8px 0}.gov-action-detail{margin-top:10px;padding-top:10px;border-top:1px solid var(--el-border-color-lighter);font-size:12px;line-height:1.6;overflow-wrap:anywhere}.gov-action-detail summary{color:var(--el-color-primary);cursor:pointer}.gov-action-detail p{margin:8px 0}.gov-action-detail details{margin:8px 0}}
</style>
<style scoped>
.mapping-filters{display:grid;gap:14px;padding:16px;border:1px solid var(--el-border-color-lighter);border-radius:12px;background:var(--el-bg-color-overlay)}
.mapping-search-row{display:flex;align-items:center;gap:12px}.mapping-search{flex:1;min-width:0;max-width:420px}.mapping-filter-row{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px}.mapping-filter-label{font-size:12px;font-weight:600;color:var(--el-text-color-secondary)}.mapping-status-scroll{overflow-x:auto;overscroll-behavior-x:contain;padding:2px 0}.mapping-status-scroll .el-radio-group{flex-wrap:nowrap;white-space:nowrap}
.mapping-note{margin:-6px 0 0;font-size:12px;line-height:1.5;color:var(--el-text-color-secondary)}.mapping-result-bar,.mapping-selection-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.mapping-result-bar strong{font-size:14px}.mapping-result-bar span{font-size:12px}.mapping-selection-bar{padding:12px 14px;border-radius:10px;background:var(--el-color-primary-light-9);border:1px solid var(--el-color-primary-light-7)}.mapping-selection-bar strong{font-size:13px}.mapping-selection-actions,.detail-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px}.mapping-selection-actions :deep(.el-button + .el-button),.detail-actions :deep(.el-button + .el-button){margin-left:0}
.detail-hero-info{min-width:0}.detail-library-counts{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;color:var(--el-text-color-secondary)}.detail-library-counts span,.detail-missing-summary span{padding:6px 9px;border-radius:7px;background:var(--el-fill-color-light)}.detail-library-counts b,.detail-missing-summary b{color:var(--el-text-color-primary)}.detail-actions{margin:8px 0 12px}.detail-note{font-size:12px;margin:8px 0;line-height:1.5}.detail-section{margin-top:24px}.detail-section-title{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}.detail-section-title strong{font-size:16px}.detail-section-title p{font-size:12px;margin:5px 0 0}.detail-missing-summary{display:flex;gap:8px;flex-wrap:wrap;font-size:12px;margin:12px 0}.detail-extra{margin-top:16px;padding:14px;border:1px solid var(--el-border-color-lighter);border-radius:10px}.detail-extra>summary,.episode-list>summary{cursor:pointer;color:var(--el-text-color-secondary);font-size:12px}.detail-extra>summary{font-size:13px;font-weight:600}.detail-extra .season-controls{margin:14px 0 0}.detail-extra p{font-size:12px}.detail-danger>summary{color:var(--el-color-danger)}.episode-list{margin-top:10px}.episode-list>summary:hover,.detail-extra>summary:hover{color:var(--el-color-primary)}
@media(max-width:639px){.mapping-filters{padding:12px;gap:12px}.mapping-search-row{align-items:stretch}.mapping-search-row>.el-button{height:auto;min-height:32px}.mapping-filter-row{gap:8px}.mapping-filter-row .mapping-filter-label:nth-of-type(2){margin-left:0}.mapping-result-bar>span{width:100%}.mapping-selection-actions{width:100%}.mapping-selection-actions :deep(.el-button){flex:1;padding-left:8px;padding-right:8px}.detail-hero{gap:12px}.detail-hero h2{font-size:18px;overflow-wrap:anywhere}.detail-actions :deep(.el-button){margin:0}.season-card{padding:12px}}
</style>
