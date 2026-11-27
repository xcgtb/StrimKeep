<script setup lang="ts">
import PageHeader from './PageHeader.vue'
import {computed, nextTick, onMounted, onUnmounted, ref, watch} from 'vue'
import {Film, Search, Setting, Refresh, Filter} from '@element-plus/icons-vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import {useRouter,useRoute} from 'vue-router'
import {ApiError} from './api/client'
const router=useRouter(),route=useRoute()
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const kind = ref<'movie' | 'tv'>('movie'), sort = ref('popular'), page = ref(1)
const genreIds = ref<number[]>([]), regions = ref<string[]>([]), year = ref<number>()
const filtersOpen=ref(false)
const filterCount = computed(() => genreIds.value.length + regions.value.length + Number(Boolean(year.value)))
type ExploreItem = { id: number; kind: 'movie' | 'tv'; title: string; poster: string; rating: number;
  year: string; badge: string; ratio?: string }
const moviesOnMobile = window.matchMedia('(max-width: 640px)')
const perPage = ref(moviesOnMobile.matches ? 30 : 45)
const resultHeading = ref<HTMLElement>()
const missingPosters = ref(new Set<number>())
const items = ref<ExploreItem[]>([]), genres = ref<{id: number; name: string}[]>([])
const totalPages=ref(1),hasMore=ref(false),busy=ref(false),error=ref(''),relaxedYear=ref(false)
const searchInput=ref(String(route.query.q||'')),activeQuery=ref(String(route.query.q||''))
watch(() => route.query.q, value => {
  const query = String(value || '').trim()
  if (query === activeQuery.value) return
  searchInput.value = activeQuery.value = query
  genreIds.value = []
  regions.value = []
  sort.value = 'popular'
  page.value = 1
  void load()
})
const badgeClass:Record<string,string>={'已入库':'badge-ok','已完整':'badge-ok','连载中':'badge-partial','未入库':'badge-none','状态未知':'badge-unknown'}

let requestVersion=0
async function load(scroll = false){
  const version=++requestVersion;busy.value=true;error.value=''
  try{
    const q=new URLSearchParams({kind:kind.value,sort:sort.value,page:String(page.value),per_page:String(perPage.value)})
    for (const genre of genreIds.value) q.append('genre', String(genre))
    for (const region of regions.value) q.append('region', region)
    if(year.value)q.set('year',String(year.value))
    if(activeQuery.value)q.set('q',activeQuery.value)
    const r=await props.api('/explore/list?'+q.toString())
    if(version!==requestVersion)return
    items.value = r.items || []
    missingPosters.value.clear()
    page.value = r.page || page.value
    totalPages.value = r.total_pages || 1
    hasMore.value = r.has_more ?? page.value < totalPages.value
    relaxedYear.value = Boolean(r.relaxed_year)
    if (scroll) {
      await nextTick()
      resultHeading.value?.scrollIntoView({block: 'start'})
    }
  }catch(e){if(version===requestVersion){error.value=(e as Error).message;items.value=[];totalPages.value=1;hasMore.value=false;relaxedYear.value=false}}finally{if(version===requestVersion)busy.value=false}
}
async function loadGenres(){
  const requested=kind.value
  try{const r=await props.api('/explore/genres?kind='+requested);if(kind.value===requested)genres.value=r.genres||[]}catch{}
}
function pick(value: 'movie' | 'tv') {
  if (kind.value === value) return
  kind.value = value
  genreIds.value = []
  genres.value = []
  if (['now_playing', 'upcoming', 'on_the_air', 'airing_today'].includes(sort.value)) sort.value = 'popular'
  page.value = 1
  void loadGenres()
  void load()
}
function clearSearchQuery() {
  if (!activeQuery.value) return
  activeQuery.value = ''
  searchInput.value = ''
  if (route.query.q) {
    const query = {...route.query}
    delete query.q
    void router.replace({query})
  }
}
function setSort(value: string) {
  if (sort.value === value) return
  clearSearchQuery()
  if (value === 'trending' && filterCount.value) {
    genreIds.value = []
    regions.value = []
    year.value = undefined
    ElMessage.info('已切换到独立的趋势榜')
  }
  sort.value = value
  page.value = 1
  void load()
}
const regionOptions = [['CN','中国大陆'],['HK','中国香港'],['TW','中国台湾'],['US','美国'],
  ['GB','英国'],['JP','日本'],['KR','韩国'],['IN','印度'],['FR','法国'],['TH','泰国']]
const activeFilters = computed(() => [
  ...genres.value.filter(genre => genreIds.value.includes(genre.id)).map(genre => genre.name),
  ...regionOptions.filter(([code]) => regions.value.includes(code)).map(([, label]) => label),
  ...(year.value ? [String(year.value) + ' 年'] : []),
])
function toggleSelection<T>(choices: T[], value: T) {
  return choices.includes(value) ? choices.filter(choice => choice !== value) : [...choices, value]
}
function applyBrowseFilters() {
  clearSearchQuery()
  if (sort.value === 'trending') sort.value = 'popular'
  page.value = 1
  void load()
}
function setRegion(code?: string) {
  if (!code && !regions.value.length) return
  regions.value = code ? toggleSelection(regions.value, code) : []
  applyBrowseFilters()
}
function setGenre(id?: number) {
  if (id === undefined && !genreIds.value.length) return
  genreIds.value = id === undefined ? [] : toggleSelection(genreIds.value, id)
  applyBrowseFilters()
}
function setYear(value?: string | number) {
  const next = value ? Number(value) : undefined
  if (next !== undefined && (!Number.isInteger(next) || next < 1900 || next > 2100)) {
    ElMessage.warning('请输入 1900 至 2100 之间的年份')
    return
  }
  if (year.value === next) return
  year.value = next
  if (sort.value === 'trending') sort.value = 'popular'
  page.value = 1
  void load()
}
function resetFilters() {
  genreIds.value = []
  regions.value = []
  year.value = undefined
  page.value = 1
  void load()
}
function go(value: number) {
  if (busy.value || value < 1 || value === page.value || (value > page.value && !hasMore.value)) return
  page.value = value
  void load(true)
}
function doSearch(){
  // 「沙丘 2021」「沙丘 (2021)」：把年份拆到筛选条件里，搜索词只留片名。
  // 整串丢给 TMDB 基本搜不到（用户反馈的现场），而且年份会显示在筛选项上，看得见。
  let text=searchInput.value.trim()
  const match=text.match(/[\(（\[]?\s*((?:19|20)\d{2})\s*[\)）\]]?$/)
  if(match){const trimmed=text.slice(0,match.index).trim();if(trimmed){text=trimmed;year.value=Number(match[1])}}
  // 名称搜索和后端筛选冲突会 422，这里直接把冲突的筛选项清掉，避免「点搜索报错/没结果」。
  genreIds.value = []
  regions.value = []
  if(sort.value!=='popular')sort.value='popular'
  searchInput.value=text
  activeQuery.value=text;page.value=1;const query={...route.query};if(text)query.q=text;else delete query.q;void router.replace({query});load()
}
function clearSearch() {
  if (!activeQuery.value) return
  clearSearchQuery()
  page.value = 1
  void load()
}

const yearOptions=computed(()=>{
  const now=new Date().getFullYear()+1,out:number[]=[]
  for(let y=now;y>=1900;y--)out.push(y)
  return out
})
const sortOptions=computed(()=>{
  const base=[{v:'popular',t:'热门'},{v:'newest',t:'最新'},{v:'top_rated',t:'高分'},{v:'trending',t:'趋势'}]
  if(kind.value==='movie')base.push({v:'now_playing',t:'正在上映'},{v:'upcoming',t:'即将上映'})
  else base.push({v:'on_the_air',t:'正在播出'},{v:'airing_today',t:'今日播出'})
  return base
})
type ResourceItem = {
  key: string
  link: string
  link_type: 'share' | 'magnet' | 'ed2k'
  title: string
  source_names: string[]
  provider_label: string
  remark: string
  published_at: string
  review_reason?: string
}
type ResourceSource = { name: string; found?: number; error?: string }
type ResourceAction = 'transfer' | 'share' | 'offline'

const resourceDialog = ref(false), selected = ref<any>(null)
const resourceItems = ref<ResourceItem[]>([]), resourceSources = ref<ResourceSource[]>([])
const resourceError = ref(''), resourceMessage = ref(''), resourceBusy = ref(false)
const submittingResource = ref(false), resourceTruncated = ref(false)
const resourceSubmissions = ref<Record<string, 'submitted' | 'uncertain'>>({})
const transferTarget = ref<any>(null), targetError = ref('')
const resourceFilter = ref<'all' | 'share' | 'offline'>('all')
let resourceRequestVersion = 0
let targetRequestVersion = 0
const resourceFailures = computed(() => resourceSources.value.filter(source => source.error))
const visibleResources = computed(() => resourceItems.value.filter(item =>
  resourceFilter.value === 'all' || (resourceFilter.value === 'share') === (item.link_type === 'share')))

type SeasonChoice = { number: number; name: string; air_date: string }
type SubscriptionSettings = { share_action: 'share' | 'transfer'; backfill: boolean; enabled: boolean }
type Subscription = SubscriptionSettings & {
  media_type: 'movie' | 'tv'; tmdb_id: number; season: number; title: string; state: string; message: string;
  last_check: number; next_check: number;
  details: { present?: number[]; missing?: number[]; submitted?: number[]; needs_review?: number[];
    candidates?: ResourceItem[]; sources?: ResourceSource[] };
  attempts: { episode: number; job_id?: number; inbox_id?: number }[];
}
const subscriptionDialog = ref(false), subscriptionBusy = ref(false), subscriptionError = ref('')
const subscriptions = ref<Subscription[]>([])
const subscriptionEditor = ref<{ id: number; kind: 'movie' | 'tv'; title: string; seasons: SeasonChoice[]; edit: boolean } | null>(null)
const subscriptionSeason = ref<number>()
const subscriptionSettings = ref<SubscriptionSettings>({share_action: 'share', backfill: false, enabled: true})
const subscribedWorks = computed(() => new Set(subscriptions.value.map(row => row.media_type + ':' + row.tmdb_id)))
const subscriptionStates: Record<string, string> = {
  pending: '等待检查', waiting: '待开播', caught_up: '已追齐', searching: '等待资源',
  review: '待核查', submitted: '等待入库', error: '检查失败'
}
const subscriptionPath = '/explore/subscriptions'
const subscriptionEndpoint = (kind: 'movie' | 'tv', id: number, season = 0) =>
  kind === 'movie' ? subscriptionPath + '/movie/' + id : subscriptionPath + '/' + id + '/' + season
const seasonLabel = (season: number) => 'S' + String(season).padStart(2, '0')
const workLabel = (row: Subscription) => row.media_type === 'movie' ? '电影' : seasonLabel(row.season)
function subscriptionLabel(row: Subscription) {
  if (row.state === 'completed') return '已入库'
  if (!row.enabled) return '已暂停'
  if (row.media_type === 'movie' && row.state === 'waiting') return '待上映'
  return subscriptionStates[row.state] || row.state
}
const episodeList = (episodes: number[]) => episodes.slice(0, 12).map(n => 'E' + String(n).padStart(2, '0')).join('、')
  + (episodes.length > 12 ? ` 等 ${episodes.length} 集` : '')
const checkTime = (ts: number) => new Date(ts * 1000).toLocaleString('zh-CN', {
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
})
function settingsFor(row: Subscription): SubscriptionSettings {
  return {share_action: row.share_action, backfill: Boolean(row.backfill), enabled: Boolean(row.enabled)}
}
async function refreshSubscriptions() {
  subscriptions.value = (await props.api(subscriptionPath)).items || []
}
async function doSubscription(action: () => Promise<void>) {
  if (subscriptionBusy.value) return
  subscriptionBusy.value = true
  subscriptionError.value = ''
  try { await action() } catch (e) {
    subscriptionError.value = (e as Error).message
  } finally {
    subscriptionBusy.value = false
  }
}
function showSubscriptions() {
  if (subscriptionBusy.value) return
  subscriptionEditor.value = null
  subscriptionDialog.value = true
  void doSubscription(refreshSubscriptions)
}
function subscribeTo(item: { id: number; kind: 'movie' | 'tv'; title: string }) {
  if (subscriptionBusy.value) return
  const movie = item.kind === 'movie' && subscriptions.value.find(row => row.media_type === 'movie' && row.tmdb_id === item.id)
  if (movie && movie.state === 'completed') {
    showSubscriptions()
    return
  }
  subscriptionError.value = ''
  subscriptionSettings.value = {share_action: 'share', backfill: false, enabled: true}
  subscriptionSeason.value = undefined
  subscriptionEditor.value = {id: item.id, kind: item.kind, title: item.title, seasons: [], edit: false}
  subscriptionDialog.value = true
  if (item.kind === 'movie') {
    if (movie) subscriptionSettings.value = settingsFor(movie)
    return
  }
  void doSubscription(async () => {
    const {title, seasons} = await props.api(`${subscriptionPath}/seasons/${item.id}`) as {
      title: string; seasons: SeasonChoice[]
    }
    subscriptionEditor.value = {id: item.id, kind: 'tv', title, seasons, edit: false}
    const today = new Date().toLocaleDateString('sv-SE')
    const aired = seasons.filter(season => season.air_date && season.air_date <= today)
    subscriptionSeason.value = aired.at(-1)?.number ?? seasons[0]?.number
    loadSeasonSettings()
  })
}
function loadSeasonSettings() {
  const row = subscriptions.value.find(row => row.media_type === 'tv'
    && row.tmdb_id === subscriptionEditor.value?.id && row.season === subscriptionSeason.value)
  subscriptionSettings.value = row ? settingsFor(row) : {share_action: 'share', backfill: false, enabled: true}
}
function editSubscription(row: Subscription) {
  subscriptionError.value = ''
  subscriptionEditor.value = {id: row.tmdb_id, kind: row.media_type, title: row.title, seasons: [], edit: true}
  subscriptionSeason.value = row.season
  subscriptionSettings.value = settingsFor(row)
}
function saveSubscription() {
  const editor = subscriptionEditor.value
  const season = subscriptionSeason.value
  if (!editor || (editor.kind === 'tv' && !season)) return
  void doSubscription(async () => {
    await props.api(subscriptionEndpoint(editor.kind, editor.id, season), 'PUT', subscriptionSettings.value)
    subscriptionEditor.value = null
    ElMessage.success('订阅已保存')
    await refreshSubscriptions()
  })
}
function updateSubscription(row: Subscription, enabled: boolean) {
  void doSubscription(async () => {
    await props.api(subscriptionEndpoint(row.media_type, row.tmdb_id, row.season), 'PUT', {...settingsFor(row), enabled})
    await refreshSubscriptions()
  })
}
async function removeSubscription(row: Subscription) {
  try {
    const label = row.media_type === 'tv' ? seasonLabel(row.season) : ''
    await ElMessageBox.confirm(`取消「${row.title}」${label}的订阅？`, '取消订阅',
      {confirmButtonText: '取消订阅', cancelButtonText: '保留', type: 'warning'})
  } catch { return }
  await doSubscription(async () => {
    await props.api(subscriptionEndpoint(row.media_type, row.tmdb_id, row.season), 'DELETE')
    await refreshSubscriptions()
  })
}
function reviewSubscription(row: Subscription) {
  ++resourceRequestVersion
  selected.value = {id: row.tmdb_id, title: row.title, kind: row.media_type}
  resourceItems.value = row.details.candidates || []
  resourceFilter.value = 'all'
  resourceSources.value = []
  resourceError.value = ''
  resourceMessage.value = ''
  resourceTruncated.value = false
  resourceBusy.value = false
  subscriptionDialog.value = false
  resourceDialog.value = true
  void loadTransferTarget()
}

async function findResources(item: any) {
  selected.value = item
  resourceDialog.value = true
  resourceMessage.value = ''
  resourceFilter.value = 'all'
  transferTarget.value = null
  targetError.value = ''
  await Promise.all([loadTransferTarget(), searchResources()])
}
async function loadTransferTarget() {
  const version = ++targetRequestVersion
  try {
    const data = await props.api('/transfer/config')
    const cfg = data.config || {}
    const target = {...cfg, target_cid: cfg.explore_cid || cfg.target_cid || '',
      target_label: cfg.explore_label || cfg.target_label || ''}
    if (version === targetRequestVersion) {
      transferTarget.value = target
      targetError.value = ''
    }
    return target
  } catch (e) {
    if (version === targetRequestVersion) {
      transferTarget.value = null
      targetError.value = (e as Error).message
    }
    return null
  }
}
async function searchResources() {
  const movie = selected.value
  if (!movie) return
  const version = ++resourceRequestVersion
  resourceBusy.value = true
  resourceError.value = ''
  resourceItems.value = []
  resourceSources.value = []
  resourceTruncated.value = false
  try {
    const result = await props.api('/explore/resources', 'POST', {
      title: movie.title, tmdb_id: movie.id, media_type: movie.kind, year: String(movie.year || '')
    })
    if (version === resourceRequestVersion) {
      resourceItems.value = result.items || []
      resourceSources.value = result.sources || []
      resourceTruncated.value = !!result.truncated
    }
  } catch (e) {
    if (version === resourceRequestVersion) resourceError.value = (e as Error).message
  } finally {
    if (version === resourceRequestVersion) resourceBusy.value = false
  }
}
async function submitResource(item: ResourceItem, action: ResourceAction) {
  if (searchOrSubmitBusy() || resourceSubmissions.value[item.key]) return
  submittingResource.value = true
  const searchVersion = resourceRequestVersion
  const actions = {
    transfer: { label: '转存到 115', path: '/transfer/jobs', description: '' },
    share: { label: '分享入库', path: '/share/ingest', description: '为该 115 分享生成 STRM，并提交整理。' },
    offline: { label: '离线下载', path: '/offline/jobs', description: '使用资源入库中已设置的离线下载目录和整理规则。' },
  }
  const operation = actions[action]
  const body: { text: string; target_cid?: string } = { text: item.link }
  try {
    if (action === 'transfer') {
      const target = await loadTransferTarget()
      if (!target?.target_cid) {
        if (searchVersion === resourceRequestVersion) {
          resourceError.value = targetError.value || '请在「资源入库 → 保存目录」设置 115 转存目录。'
        }
        return
      }
      const label = target.target_label || '所选 115 目录'
      operation.description = `保存至「${label}」。${target.auto_organize ? '转存核实后自动整理。' : '转存后由手动整理或定时扫描跟进。'}`
      body.target_cid = String(target.target_cid)
    }
    if (searchVersion !== resourceRequestVersion || !resourceDialog.value) return
    try {
      await ElMessageBox.confirm(operation.description, operation.label,
        { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' })
    } catch { return }
    if (searchVersion !== resourceRequestVersion || !resourceDialog.value) return
    resourceError.value = ''
    resourceMessage.value = ''
    try {
      const result = await props.api(operation.path, 'POST', body)
      resourceSubmissions.value[item.key] = 'submitted'
      if (searchVersion !== resourceRequestVersion) return
      if (action === 'share') {
        resourceMessage.value = `分享库新建 ${result.count} 个 STRM。` +
          (result.job_id ? `整理任务 #${result.job_id} 已入队。` : '')
        if (result.organize_error) resourceError.value = result.organize_error
      } else {
        resourceMessage.value = action === 'offline'
          ? `离线下载已提交，记录 #${result.submission_id}。`
          : `转存任务${result.items?.[0]?.id ? ' #' + result.items[0].id : ''}已入队。`
      }
    } catch (e) {
      const rejected = e instanceof ApiError && e.status >= 400 && e.status < 500
      if (!rejected) resourceSubmissions.value[item.key] = 'uncertain'
      if (searchVersion === resourceRequestVersion) {
        resourceError.value = rejected ? e.message
          : `${operation.label}结果未确认，请先在任务中心核查。${(e as Error).message}`
      }
    }
  } finally {
    submittingResource.value = false
  }
}
const searchOrSubmitBusy = () => resourceBusy.value || submittingResource.value
async function copyResource(item: ResourceItem) {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(item.link)
    else {
      const input = document.createElement('textarea')
      input.value = item.link
      input.style.position = 'fixed'
      input.style.opacity = '0'
      document.body.appendChild(input)
      input.select()
      try {
        if (!document.execCommand('copy')) throw new Error('浏览器不支持复制，请在安全连接下重试。')
      } finally { input.remove() }
    }
    ElMessage.success('链接已复制')
  } catch (e) { ElMessage.error((e as Error).message) }
}
const poster=(p:string)=>p?`https://image.tmdb.org/t/p/w500${p}`:''
function updatePageSize(event: MediaQueryListEvent) {
  perPage.value = event.matches ? 30 : 45
  page.value = 1
  void load()
}
let lastInventoryResource = ''
watch(() => [route.name, route.query.resource_id, route.query.resource_kind, route.query.q], () => {
  if (route.name !== 'explore') { lastInventoryResource = ''; return }
  const id = String(route.query.resource_id || ''), kind = String(route.query.resource_kind || ''), title = String(route.query.q || '').trim()
  if (!/^\d+$/.test(id) || !['tv', 'movie'].includes(kind) || !title) { lastInventoryResource = ''; return }
  const key = `${kind}:${id}:${title}`
  if (key === lastInventoryResource) return
  lastInventoryResource = key
  void findResources({id:Number(id), kind, title})
}, {immediate:true})
onMounted(() => {
  moviesOnMobile.addEventListener('change', updatePageSize)
  void refreshSubscriptions().catch(() => {})
  void loadGenres()
  void load()
})
onUnmounted(() => moviesOnMobile.removeEventListener('change', updatePageSize))
</script>
<template>
<PageHeader title="影视探索" description="搜索电影和剧集，查看两库的入库状态。"><template #actions>
<div class="kind-switch" aria-label="作品类型"><button :class="{active:kind==='movie'}" :aria-pressed="kind==='movie'" @click="pick('movie')">电影</button><button :class="{active:kind==='tv'}" :aria-pressed="kind==='tv'" @click="pick('tv')">剧集</button></div><el-button @click="showSubscriptions">追更订阅</el-button></template></PageHeader>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>

<section class="filter-bar" aria-label="影视搜索与筛选">
  <div class="discovery-toolbar">
    <form class="search-row" @submit.prevent="doSearch">
      <el-input v-model="searchInput" :prefix-icon="Search" aria-label="搜索电影或剧集" placeholder="输入片名或关键词"/>
      <el-button type="primary" native-type="submit">搜索</el-button>
    </form>
    <div class="discovery-controls">
      <el-select class="sort-select" :model-value="sort" aria-label="浏览排序" @change="setSort">
        <el-option v-for="option in sortOptions" :key="option.v" :label="option.t" :value="option.v"/>
      </el-select>
      <el-button :icon="Filter" :aria-expanded="filtersOpen" aria-controls="explore-filters" @click="filtersOpen=!filtersOpen">
        筛选<span v-if="filterCount" class="filter-count">{{filterCount}}</span>
      </el-button>
    </div>
  </div>
  <div v-show="filtersOpen" id="explore-filters" class="filter-block">
    <div class="filter-row" role="group" aria-label="影视类型">
      <span class="filter-label">类型</span>
      <div class="filter-options">
        <button type="button" class="filter-chip" :aria-pressed="!genreIds.length" @click="setGenre()">全部类型</button>
        <button v-for="genre in genres" :key="genre.id" type="button" class="filter-chip"
          :aria-pressed="genreIds.includes(genre.id)" @click="setGenre(genre.id)">{{genre.name}}</button>
      </div>
    </div>
    <div class="filter-row" role="group" aria-label="制作地区">
      <span class="filter-label">地区</span>
      <div class="filter-options">
        <button type="button" class="filter-chip" :aria-pressed="!regions.length" @click="setRegion()">全部地区</button>
        <button v-for="[code, label] in regionOptions" :key="code" type="button" class="filter-chip"
          :aria-pressed="regions.includes(code)" @click="setRegion(code)">{{label}}</button>
      </div>
    </div>
    <div class="filter-row">
      <span class="filter-label">年份</span>
      <div class="year-filter">
        <el-select :model-value="year" filterable allow-create clearable default-first-option
          aria-label="上映或首播年份" placeholder="全部年份" @change="setYear">
          <el-option v-for="option in yearOptions" :key="option" :label="String(option)" :value="option"/>
        </el-select>
        <el-button v-if="filterCount" text @click="resetFilters">清除筛选</el-button>
      </div>
    </div>
    <p class="filter-hint">类型和地区可多选。同一组选中任意一项即可匹配。</p>
  </div>
  <div v-if="filterCount && !filtersOpen" class="filter-summary">
    <span v-for="label in activeFilters" :key="label" class="filter-selection">{{label}}</span>
    <el-button text size="small" @click="resetFilters">清除筛选</el-button>
  </div>
</section>

<div ref="resultHeading" class="results-header">
  <div>
    <span v-if="activeQuery" class="search-tag">{{activeQuery}} <button type="button" aria-label="清除搜索" @click="clearSearch">×</button></span>
    <span v-if="relaxedYear" class="muted result-note">该年份没有匹配，已显示不限年份的结果</span>
  </div>
  <span role="status" class="muted count">{{busy ? '正在加载' : `${items.length} 部 · 第 ${page} / ${totalPages} 页`}}</span>
</div>

<el-skeleton v-if="!items.length&&busy" :rows="6" animated/>
<el-empty v-else-if="!items.length" :description="activeQuery?'没有找到「'+activeQuery+'」的相关结果':'没有结果'"/>
<div v-else class="explore-grid" :aria-busy="busy">
<article v-for="it in items" :key="it.kind+':'+it.id" class="explore-card">
<div class="poster-image">
<img v-if="it.poster && !missingPosters.has(it.id)" :src="poster(it.poster)" loading="lazy" decoding="async"
  :alt="it.title" @error="missingPosters.add(it.id)"/>
<div v-else class="poster-placeholder" aria-label="暂无海报" role="img"><el-icon><Film/></el-icon></div>
<span v-if="it.rating" class="ov rating" aria-label="评分">{{it.rating.toFixed(1)}}</span>
<span :class="['ov status',badgeClass[it.badge]||'badge-none']">{{it.badge}}{{it.ratio?' '+it.ratio:''}}</span>
</div>
<div class="explore-info">
<h3 :title="it.title">{{it.title}}</h3>
<p class="muted card-year">{{it.year}}</p>
<div class="card-actions">
<el-button size="small" @click="findResources(it)">查找资源</el-button>
<el-button class="subscribe-card-button" size="small" @click="subscribeTo(it)">
  {{subscribedWorks.has(it.kind + ':' + it.id) ? '订阅设置' : it.kind === 'movie' ? '订阅电影' : '订阅追更'}}
</el-button>
</div>
</div>
</article>
</div>

<nav v-if="totalPages>1" class="pagination" aria-label="影视探索分页">
<el-button :disabled="busy || page<=1" @click="go(page-1)">上一页</el-button>
<span>第 {{page}} / {{totalPages}} 页</span>
<el-button :disabled="busy || !hasMore" @click="go(page+1)">下一页</el-button>
</nav>

<el-dialog v-model="subscriptionDialog" class="discovery-dialog" width="min(660px, 96vw)"
  :title="subscriptionEditor?.title || '追更订阅'">
  <el-alert v-if="subscriptionError" :title="subscriptionError" type="error" :closable="false" show-icon/>
  <el-form v-if="subscriptionEditor" v-loading="subscriptionBusy" label-position="top" class="subscription-editor">
    <el-form-item v-if="subscriptionEditor.kind==='tv'" label="订阅季">
      <el-select v-if="!subscriptionEditor.edit" v-model="subscriptionSeason" placeholder="选择季" @change="loadSeasonSettings">
        <el-option v-for="season in subscriptionEditor.seasons" :key="season.number" :value="season.number"
          :label="`${seasonLabel(season.number)} · ${season.name || '第 ' + season.number + ' 季'}`"/>
      </el-select>
      <span v-else>{{ seasonLabel(subscriptionSeason || 1) }}</span>
    </el-form-item>
    <el-form-item label="115 分享入库方式">
      <el-radio-group v-model="subscriptionSettings.share_action">
        <el-radio value="share">生成分享 STRM</el-radio>
        <el-radio value="transfer">转存到 115</el-radio>
      </el-radio-group>
      <p class="hint">磁力和 ED2K 使用资源入库中已配置的离线目录。</p>
    </el-form-item>
    <el-form-item v-if="subscriptionEditor.kind==='tv'">
      <el-checkbox v-model="subscriptionSettings.backfill">补齐已有集数之前的缺集</el-checkbox>
    </el-form-item>
    <div class="subscription-actions">
      <el-button @click="showSubscriptions">订阅列表</el-button>
      <el-button type="primary" :disabled="subscriptionEditor.kind==='tv' && !subscriptionSeason" :loading="subscriptionBusy" @click="saveSubscription">保存订阅</el-button>
    </div>
  </el-form>
  <div v-else v-loading="subscriptionBusy" class="subscription-list">
    <div class="subscription-actions"><el-button :icon="Refresh" :disabled="subscriptionBusy" @click="doSubscription(refreshSubscriptions)">刷新</el-button></div>
    <el-empty v-if="!subscriptions.length && !subscriptionBusy" description="从电影或剧集卡片添加订阅"/>
    <article v-for="row in subscriptions" :key="`${row.media_type}:${row.tmdb_id}:${row.season}`" class="subscription-row">
      <header>
        <strong>{{row.title}} · {{workLabel(row)}}</strong>
        <el-tag size="small" :type="row.state === 'completed' ? 'success' : row.enabled && ['review','error'].includes(row.state) ? 'warning' : 'info'">
          {{subscriptionLabel(row)}}
        </el-tag>
      </header>
      <p v-if="row.message" class="hint">{{row.message}}</p>
      <p class="hint" v-if="row.last_check">上次检查 {{checkTime(row.last_check)}}<span v-if="row.enabled && row.next_check"> · 下次 {{checkTime(row.next_check)}}</span></p>
      <template v-if="row.media_type==='tv'">
        <p class="hint" v-if="row.details.present?.length">已入库 {{episodeList(row.details.present)}}</p>
        <p class="hint" v-if="row.details.missing?.length">待补 {{episodeList(row.details.missing)}}</p>
        <p class="hint" v-if="row.details.submitted?.length">已提交 {{episodeList(row.details.submitted)}}</p>
        <p class="hint" v-if="row.details.needs_review?.length">待核查 {{episodeList(row.details.needs_review)}}</p>
      </template>
      <p class="hint" v-if="row.details.sources?.some(source => source.error)">
        搜索失败 {{row.details.sources.filter(source => source.error).map(source => source.name).join('、')}}
      </p>
      <p class="hint" v-if="row.attempts.length">提交记录 {{row.attempts.slice(-4).map(a =>
        (row.media_type === 'tv' ? 'E' + a.episode + ' · ' : '') + (a.job_id ? '任务 #' + a.job_id : a.inbox_id ? '收件箱 #' + a.inbox_id : '已有分享 STRM')).join('、')}}</p>
      <div class="subscription-actions">
        <el-button v-if="row.details.candidates?.length" size="small" @click="reviewSubscription(row)">查看候选</el-button>
        <el-button v-else-if="row.details.needs_review?.length" size="small"
          @click="subscriptionDialog=false;findResources({id:row.tmdb_id,title:row.title,kind:row.media_type})">查找其他资源</el-button>
        <el-button v-if="row.state!=='completed'" size="small" @click="editSubscription(row)">编辑</el-button>
        <el-button v-if="row.state!=='completed'" size="small" @click="updateSubscription(row, !row.enabled)">{{row.enabled ? '暂停' : '恢复'}}</el-button>
        <el-button v-if="row.enabled" size="small" @click="updateSubscription(row, true)">安排检查</el-button>
        <el-button size="small" text type="danger" @click="removeSubscription(row)">取消订阅</el-button>
      </div>
    </article>
  </div>
</el-dialog>

<el-dialog v-model="resourceDialog" class="discovery-dialog" width="min(880px, 96vw)" :show-close="true">
<template #header><div class="resource-heading"><h2>{{selected?.title}}</h2><p>{{selected?.kind==='tv'?'剧集':'电影'}} · {{selected?.year||'年份未知'}} · {{selected?.badge||'状态未知'}}</p></div></template>
<div class="resource-toolbar">
  <span class="hint">{{ resourceBusy ? '正在搜索已启用的来源…' : resourceSources.length ? `已搜索 ${resourceSources.length} 个来源` : '聚合搜索' }}</span>
  <div class="resource-toolbar-actions">
    <el-button :icon="Refresh" :loading="resourceBusy" :disabled="submittingResource" @click="searchResources">刷新</el-button>
    <el-button :icon="Setting" @click="resourceDialog=false;router.push({name:'settings',query:{section:'resource'}})">资源来源</el-button>
  </div>
</div>
<el-alert v-if="resourceError" :title="resourceError" type="error" show-icon :closable="false"/>
<el-alert v-if="resourceTruncated" title="结果较多，仅显示前 100 条。" type="info" show-icon :closable="false"/>
<el-alert v-if="resourceFailures.length" :title="resourceFailures.map(source => source.name + ' · ' + source.error).join('；')" type="warning" show-icon :closable="false"/>
<el-alert v-if="resourceMessage" :title="resourceMessage" type="success" show-icon :closable="false"/>
<div class="target-summary">
  <div><span class="target-caption">115 转存目录</span><strong>{{ transferTarget?.target_label || (transferTarget?.target_cid ? '已设置' : '未设置') }}</strong>
    <small v-if="transferTarget?.target_cid">{{ transferTarget.auto_organize ? '转存后自动整理' : '手动整理或定时扫描' }}</small>
    <small v-else-if="targetError">{{ targetError }}</small>
  </div>
  <el-button v-if="!transferTarget?.target_cid" size="small" @click="resourceDialog=false;router.push('/transfer')">去设置</el-button>
</div>
<div class="resource-summary">
  <div><strong>{{ resourceItems.length }}</strong><span> 个资源</span></div>
  <div class="resource-filters">
    <button :class="{active:resourceFilter==='all'}" @click="resourceFilter='all'">全部</button>
    <button :class="{active:resourceFilter==='share'}" @click="resourceFilter='share'">115 分享</button>
    <button :class="{active:resourceFilter==='offline'}" @click="resourceFilter='offline'">磁力 / ED2K</button>
  </div>
</div>
<el-skeleton v-if="resourceBusy" :rows="4" animated/>
<el-empty v-else-if="!visibleResources.length && !resourceError"
  :description="resourceItems.length ? '当前筛选下没有资源' : resourceFailures.length === resourceSources.length && resourceFailures.length ? '所有来源查询失败' : '暂未找到资源'"/>
<div v-else class="resource-list">
  <article v-for="item in visibleResources" :key="item.key" class="resource-card">
    <div class="resource-labels">
      <span v-for="name in item.source_names" :key="name" class="source-badge">{{ name }}</span>
      <span>{{ item.provider_label }}</span>
    </div>
    <h3>{{ item.title }}</h3>
    <p v-if="item.review_reason" class="resource-remark">{{ item.review_reason }}</p>
    <p v-if="item.remark && item.remark !== item.title" class="resource-remark" :title="item.remark">{{ item.remark }}</p>
    <span v-if="item.published_at" class="resource-meta">{{ item.published_at }}</span>
    <div class="resource-card-actions">
      <span v-if="resourceSubmissions[item.key]" class="resource-note">{{ resourceSubmissions[item.key] === 'submitted' ? '已提交 · 查看任务中心' : '结果需核查' }}</span>
      <div class="resource-buttons">
        <el-button size="small" @click="copyResource(item)">复制链接</el-button>
        <template v-if="item.link_type === 'share'">
          <el-button size="small" :disabled="searchOrSubmitBusy() || !!resourceSubmissions[item.key]" @click="submitResource(item, 'share')">分享入库</el-button>
          <el-button type="primary" size="small" :disabled="searchOrSubmitBusy() || !transferTarget?.target_cid || !!resourceSubmissions[item.key]" @click="submitResource(item, 'transfer')">转存到 115</el-button>
        </template>
        <el-button v-else type="primary" size="small" :disabled="searchOrSubmitBusy() || !!resourceSubmissions[item.key]" @click="submitResource(item, 'offline')">离线下载</el-button>
      </div>
    </div>
  </article>
</div>
</el-dialog>
</template>
<style scoped>
:global(.discovery-dialog){margin:4vh auto !important;max-height:92vh;display:flex;flex-direction:column}:global(.discovery-dialog .el-dialog__body){min-height:0;overflow:auto}
.subscription-editor,.subscription-list{padding-top:12px}
.subscription-row{padding:14px 0;border-top:1px solid var(--line)}
.subscription-row header,.subscription-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.subscription-row header{justify-content:space-between}
.subscription-row .hint,.subscription-editor .hint{margin:6px 0}
.subscription-list>.subscription-actions{justify-content:flex-end;margin-bottom:12px}
.subscription-row .subscription-actions{margin-top:9px}
.subscription-actions :deep(.el-button){margin-left:0}

.target-summary{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 13px;border:1px solid var(--el-border-color-lighter);background:var(--el-fill-color-light);border-radius:10px}.target-summary>div{display:flex;align-items:baseline;flex-wrap:wrap;gap:6px 10px}.target-caption{font-size:10px;letter-spacing:.06em;color:var(--el-color-primary);font-weight:700}.target-summary strong{font-size:13px}.target-summary small{font-size:12px;color:var(--el-text-color-secondary)}.resource-buttons{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.resource-buttons :deep(.el-button + .el-button){margin-left:0}.resource-heading{padding-right:32px}.resource-heading h2{font-size:22px;margin:5px 0 4px;line-height:1.3}.resource-heading p{margin:0;color:var(--el-text-color-secondary);font-size:13px}.resource-toolbar,.resource-summary,.resource-card-actions{display:flex;align-items:center;justify-content:space-between;gap:12px}.resource-toolbar{padding:4px 0 18px}.resource-toolbar-actions{display:flex;align-items:center;gap:10px}.resource-summary{padding:17px 0 12px;border-top:1px solid var(--el-border-color-lighter);font-size:13px;color:var(--el-text-color-secondary)}.resource-summary strong{font-size:20px;color:var(--el-text-color-primary)}.resource-filters{display:flex;gap:4px;background:var(--el-fill-color-light);padding:3px;border-radius:9px}.resource-filters button{border:0;background:transparent;color:var(--el-text-color-secondary);font-size:12px;padding:6px 9px;border-radius:7px;cursor:pointer}.resource-filters button.active{background:var(--el-bg-color);color:var(--el-color-primary);box-shadow:0 1px 4px #0002}.resource-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;max-height:52vh;overflow:auto;padding:1px 4px 4px 1px}.resource-card{border:1px solid var(--el-border-color-lighter);background:var(--el-fill-color-blank);border-radius:12px;padding:14px;min-width:0;display:flex;flex-direction:column;gap:10px}.resource-card h3{font-size:14px;line-height:1.5;margin:0;overflow-wrap:anywhere}.resource-labels,.resource-meta{display:flex;flex-wrap:wrap;gap:6px}.resource-labels span{font-size:11px;padding:3px 7px;border-radius:5px;background:var(--el-fill-color-light);color:var(--el-text-color-regular)}.resource-labels .source-badge{color:var(--el-color-primary);background:var(--el-color-primary-light-9);font-weight:700}.resource-meta{font-size:12px;color:var(--el-text-color-secondary)}.resource-card-actions{margin-top:auto;padding-top:10px;border-top:1px solid var(--el-border-color-lighter)}.resource-note{font-size:12px;color:var(--el-text-color-secondary)}.resource-remark{margin:0;font-size:12px;line-height:1.6;color:var(--el-text-color-regular);overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.badge-unknown{background:rgba(130,108,48,.9)}
@media(max-width:640px){.resource-list{grid-template-columns:1fr;max-height:48vh}.resource-toolbar,.resource-summary,.resource-card-actions{align-items:flex-start;flex-wrap:wrap}.resource-heading h2{font-size:19px}.resource-filters{margin-left:auto}}
.kind-switch {
  display: flex;
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface);
}
.kind-switch button {
  min-width: 58px;
  border: 0;
  border-radius: 6px;
  padding: 7px 12px;
  color: var(--text);
  background: transparent;
  cursor: pointer;
}
.kind-switch button.active { background: var(--accent); color: #fff; }
.filter-bar {
  margin-bottom: 18px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
  padding: 14px;
}
.discovery-toolbar, .search-row, .discovery-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}
.discovery-toolbar { justify-content: space-between; }
.search-row { flex: 1; max-width: 460px; min-width: 0; }
.search-row :deep(.el-input) { min-width: 0; }
.discovery-controls { flex-shrink: 0; }
.sort-select { width: 128px; }
.discovery-toolbar :deep(.el-button) { height: 36px; margin: 0; border-radius: 8px; }
.discovery-toolbar :deep(.el-input__wrapper), .discovery-toolbar :deep(.el-select__wrapper) { min-height: 36px; }
.filter-count {
  margin-left: 6px;
  min-width: 18px;
  border-radius: 10px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 11px;
  line-height: 18px;
}
.filter-block {
  display: grid;
  gap: 12px;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--line);
}
.filter-row { display: grid; grid-template-columns: 3em minmax(0,1fr); align-items: start; gap: 8px; }
.filter-label { padding-top: 7px; font-size: 13px; font-weight: 600; color: var(--muted); }
.filter-options, .filter-summary, .year-filter { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.filter-chip, .filter-selection {
  border: 1px solid transparent;
  border-radius: 7px;
  padding: 6px 10px;
  background: var(--surface-soft);
  color: var(--text);
  font-size: 12px;
}
.filter-chip { min-height: 32px; cursor: pointer; }
.filter-chip:hover { border-color: var(--el-color-primary-light-5); }
.filter-chip[aria-pressed='true'] {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-5);
  color: var(--el-color-primary);
  font-weight: 600;
}
.filter-chip:focus-visible, .kind-switch button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.year-filter :deep(.el-select) { width: 135px; }
.filter-hint { margin: 0 0 0 4em; color: var(--muted); font-size: 12px; line-height: 1.5; }
.filter-summary { margin-top: 11px; }
.filter-summary :deep(.el-button) { margin-left: 0; }
.results-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 26px;
  gap: 10px;
  margin: 0 0 10px;
  scroll-margin-top: 84px;
}
.results-header > div { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 9px; min-width: 0; }
.result-note, .count { font-size: 12px; }
.count { flex-shrink: 0; }
.search-tag {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  max-width: 100%;
  border-radius: 6px;
  padding: 4px 8px;
  color: var(--accent);
  background: var(--surface-soft);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.search-tag button { border: 0; padding: 0 2px; background: none; color: inherit; font-size: 16px; cursor: pointer; }
.explore-grid {
  display: grid;
  grid-template-columns: repeat(3,minmax(0,1fr));
  align-items: stretch;
  gap: 14px;
}
.explore-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  transition: border-color .16s, box-shadow .16s;
}
.explore-card:focus-within, .explore-card:hover { border-color: var(--el-color-primary-light-5); box-shadow: var(--panel-shadow); }
.poster-image { position: relative; aspect-ratio: 2/3; overflow: hidden; background: var(--surface-soft); }
.poster-image img { display: block; width: 100%; height: 100%; object-fit: cover; }
.poster-placeholder { display: grid; height: 100%; place-items: center; color: var(--muted); font-size: 26px; }
.ov {
  position: absolute;
  border-radius: 5px;
  max-width: calc(100% - 12px);
  padding: 3px 6px;
  color: #fff;
  font-size: 10px;
  line-height: 1.5;
  font-weight: 600;
  white-space: nowrap;
  text-overflow: ellipsis;
  overflow: hidden;
}
.ov.rating { top: 6px; right: 6px; background: rgba(15,23,42,.8); color: #fbbf24; }
.ov.status { bottom: 6px; left: 6px; }
.status.badge-ok { background: rgba(29,112,77,.95); }
.status.badge-partial { background: rgba(36,99,166,.95); }
.status.badge-none { background: rgba(57,65,78,.93); }
.explore-info { flex: 1; display: flex; flex-direction: column; padding: 9px; gap: 4px; min-width: 0; }
.explore-info h3 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  overflow-wrap: anywhere;
  min-height: 2.8em;
  margin: 0;
  font-size: 14px;
  line-height: 1.4;
  color: var(--text);
}
.card-year { min-height: 1rem; margin: 0; font-size: 11px; }
.card-actions { display: grid; gap: 5px; margin-top: auto; padding-top: 4px; }
.card-actions :deep(.el-button) { height: 33px; margin: 0; padding: 0 4px; border-radius: 6px; }
.card-actions :deep(.subscribe-card-button) { color: var(--accent); }
.pagination { display: flex; align-items: center; justify-content: center; gap: 10px; margin-top: 22px; font-size: 12px; color: var(--muted); }
.pagination :deep(.el-button) { margin: 0; }
.resource-heading h2 { letter-spacing: -.35px; }
.target-summary { border-radius: 8px; background: color-mix(in srgb,var(--accent) 5%,var(--surface)); border-color: var(--line); }
.resource-card { border-color: var(--line); border-radius: 9px; padding: 16px; box-shadow: 0 3px 12px rgba(32,55,85,.025); }
.resource-card h3 { font-size: 15px; }
.resource-list { max-height: none; overflow: visible; padding: 1px; }
.resource-card-actions { align-items: center; flex-wrap: wrap; gap: 8px; }
.resource-buttons { margin-left: auto; }
.resource-buttons :deep(.el-button) { min-height: 34px; padding-inline: 12px; border-radius: 7px; }
@media (min-width: 960px) {
  .explore-grid { grid-template-columns: repeat(5,minmax(0,1fr)); }
}
@media (min-width: 1200px) {
  .card-actions { grid-template-columns: repeat(2,minmax(0,1fr)); }
}
@media (max-width: 640px) {
  .filter-bar { padding: 11px; margin-bottom: 13px; }
  .discovery-toolbar { flex-wrap: wrap; }
  .search-row { flex: 1 0 100%; max-width: none; }
  .discovery-controls { flex: 1; justify-content: space-between; }
  .sort-select { flex: 1; }
  .filter-row { grid-template-columns: 1fr; gap: 5px; }
  .filter-label { padding-top: 0; }
  .filter-hint { margin-left: 0; }
  .filter-chip { min-height: 34px; }
  .explore-grid { gap: 8px; }
  .explore-info { padding: 7px; }
  .explore-info h3 { font-size: 12px; }
  .card-actions :deep(.el-button) { height: 34px; font-size: 11px; }
  .resource-card { padding: 13px; }
  .resource-list { max-height: none; overflow: visible; }
  .resource-toolbar-actions { flex-wrap: wrap; }
  .resource-card-actions { align-items: stretch; }
  .resource-note { width: 100%; }
  .resource-buttons { width: 100%; margin-left: 0; }
  .resource-buttons :deep(.el-button) { flex: 1; min-width: 0; min-height: 40px; padding-inline: 8px; }
}
</style>
