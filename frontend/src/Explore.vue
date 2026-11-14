<script setup lang="ts">
import {ref,onMounted,computed,watch} from 'vue'
import {Film, Search, Setting, Refresh} from '@element-plus/icons-vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import {useRouter,useRoute} from 'vue-router'
const router=useRouter(),route=useRoute()
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const kind=ref<'movie'|'tv'>('movie'),sort=ref('popular'),genre=ref<number|undefined>(),year=ref<number|undefined>(),region=ref(''),page=ref(1)
const filtersOpen=ref(false)
const filterCount=computed(()=>Number(sort.value!=='popular')+Number(!!genre.value)+Number(!!year.value)+Number(!!region.value) )
const items=ref<any[]>([]),genres=ref<any[]>([]),totalPages=ref(1),busy=ref(false),error=ref('')
const searchInput=ref(String(route.query.q||'')),activeQuery=ref(String(route.query.q||'')),yearInput=ref('')
watch(()=>route.query.q,value=>{const q=String(value||'').trim();if(q!==activeQuery.value){searchInput.value=q;activeQuery.value=q;genre.value=undefined;region.value='';sort.value='popular';page.value=1;void load()}})
const badgeClass:Record<string,string>={'已入库':'badge-ok','已完整':'badge-ok','连载中':'badge-partial','未入库':'badge-none','状态未知':'badge-unknown'}

let requestVersion=0
async function load(){
  const version=++requestVersion;busy.value=true;error.value=''
  try{
    const q=new URLSearchParams({kind:kind.value,sort:sort.value,page:String(page.value),per_page:String(perPage)})
    if(genre.value)q.set('genre',String(genre.value))
    if(year.value)q.set('year',String(year.value))
    if(region.value)q.set('region',region.value)
    if(activeQuery.value)q.set('q',activeQuery.value)
    const r=await props.api('/explore/list?'+q.toString())
    if(version!==requestVersion)return
    const seen=new Set<string>()
    items.value=(r.items||[]).filter((item:any)=>{const key=item.kind+':'+item.id;if(seen.has(key))return false;seen.add(key);return true});totalPages.value=r.total_pages||1
    error.value=''
  }catch(e){if(version===requestVersion){error.value=(e as Error).message;items.value=[];totalPages.value=1}}finally{if(version===requestVersion)busy.value=false}
}
async function loadGenres(){
  const requested=kind.value
  try{const r=await props.api('/explore/genres?kind='+requested);if(kind.value===requested)genres.value=r.genres||[]}catch{}
}
function pick(k:'movie'|'tv'){if(kind.value===k)return;kind.value=k;genre.value=undefined;if(['now_playing','upcoming','on_the_air','airing_today'].includes(sort.value))sort.value='popular';page.value=1;loadGenres();load()}
function leaveSearchForBrowse(){if(activeQuery.value){activeQuery.value='';searchInput.value='';if(route.query.q){const query={...route.query};delete query.q;void router.replace({query})}ElMessage.info('已退出名称搜索，按筛选条件浏览')}}
function setSort(s:string){if(sort.value===s)return;leaveSearchForBrowse();sort.value=s;page.value=1;load()}
const regionOptions=[['CN','中国大陆'],['HK','中国香港'],['TW','中国台湾'],['US','美国'],['GB','英国'],['JP','日本'],['KR','韩国'],['IN','印度'],['FR','法国'],['TH','泰国']]
function setRegion(code:string){if(region.value===code)return;leaveSearchForBrowse();region.value=code;page.value=1;load()}
function setGenre(g:number|undefined){if(genre.value===g)return;leaveSearchForBrowse();genre.value=g;page.value=1;load()}
function setYear(y:number|undefined){if(year.value===y)return;year.value=y;yearInput.value=y?String(y):'';page.value=1;load()}
function applyYear(){
  const text=yearInput.value.trim()
  if(!text){setYear(undefined);return}
  const v=Number(text)
  if(Number.isInteger(v)&&v>=1900&&v<=2100)setYear(v)
  else yearInput.value=year.value?String(year.value):''
}
function go(p:number){if(p<1||p>totalPages.value||p===page.value)return;page.value=p;load()}
function doSearch(){
  // 「沙丘 2021」「沙丘 (2021)」：把年份拆到筛选条件里，搜索词只留片名。
  // 整串丢给 TMDB 基本搜不到（用户反馈的现场），而且年份会显示在筛选项上，看得见。
  let text=searchInput.value.trim()
  const match=text.match(/[\(（\[]?\s*((?:19|20)\d{2})\s*[\)）\]]?$/)
  if(match){const trimmed=text.slice(0,match.index).trim();if(trimmed){text=trimmed;const guess=Number(match[1]);if(year.value!==guess){year.value=guess;yearInput.value=String(guess)}}}
  // 名称搜索和后端筛选冲突会 422，这里直接把冲突的筛选项清掉，避免「点搜索报错/没结果」。
  if(genre.value!==undefined)genre.value=undefined
  if(region.value)region.value=''
  if(sort.value!=='popular')sort.value='popular'
  searchInput.value=text
  activeQuery.value=text;page.value=1;const query={...route.query};if(text)query.q=text;else delete query.q;void router.replace({query});load()
}
function clearSearch(){if(!activeQuery.value)return;searchInput.value='';activeQuery.value='';const query={...route.query};delete query.q;void router.replace({query});page.value=1;load()}

const yearOptions=computed(()=>{
  const now=new Date().getFullYear(),out:number[]=[]
  for(let y=now;y>=now-14;y--)out.push(y)
  return out
})
const sortOptions=computed(()=>{
  const base=[{v:'popular',t:'🔥 热门'},{v:'trending',t:'📈 趋势'},{v:'top_rated',t:'⭐ 高分'},{v:'newest',t:'🆕 最新'}]
  if(kind.value==='movie')base.push({v:'now_playing',t:'🎬 正在上映'},{v:'upcoming',t:'📅 即将上映'})
  else base.push({v:'on_the_air',t:'📺 正在播出'},{v:'airing_today',t:'📅 今日播出'})
  return base
})
const resourceDialog=ref(false), selected=ref<any>(null)
const resourceItems=ref<any[]>([]), resourceError=ref(''), resourceBusy=ref(false), resourcePartial=ref(false), resourceTruncated=ref(false)
const resourceStates=new Map<string,any>()
function resourceKey(item:any){return item.slug?`${item.source}|${item.provider}|${item.slug}`:''}
function rememberResource(item:any){const key=resourceKey(item);if(key)resourceStates.set(key,{share_url:item.share_url,resolveUncertain:item.resolveUncertain,transferUncertain:item.transferUncertain,transferSubmitted:item.transferSubmitted,shareSubmitted:item.shareSubmitted})}
function restoreResource(item:any){const state=resourceStates.get(resourceKey(item));if(!state)return item;return {...item,...state,share_url:state.share_url||item.share_url}}
const resourceSource=ref('re0'), resourceMessage=ref('')
let resourceRequestVersion=0
const transferTarget=ref<any>(null), targetError=ref('')
const resourceFilter=ref('all')
const visibleResources=computed(()=>resourceItems.value.filter((item:any)=>resourceFilter.value==='all'||(resourceFilter.value==='115'&&item.provider==='115')||(resourceFilter.value==='ready'&&!!item.share_url)))
function sourceLabel(value:string){return ({re0:'RE0',guanying:'观影',seedhub:'SeedHub'} as Record<string,string>)[value]||value||'来源未知'}
function resourceTags(item:any){return [...new Set([item.resolution,item.quality,...(item.resource_spec_tags||[]),...(item.technical_tags||[]),...(item.subtitle_languages||[])].filter(Boolean))].slice(0,7)}
// 备注是发布者写的版本说明（字幕、修复哪些集、片源），资源卡必须显示，
// 不然只看标题和体积无法判断这个版本能不能用。
function resourceRemark(item:any){return String(item.remark||item.message_text||item.message_body||'').trim()}
function resourceDetails(item:any){
  const rows:[string,string][]=[]
  const push=(label:string,value:any)=>{const text=Array.isArray(value)?value.filter(Boolean).join('、'):String(value??'').trim();if(text)rows.push([label,text])}
  push('发布者',item.sharer)
  push('发布时间',item.published_at)
  push('规格',item.resource_specification)
  push('字幕',[...(item.subtitle_types||[]),...(item.subtitle_languages||[])])
  push('片源',item.source_tags)
  push('校验',[item.validation_label,item.validate_message])
  if(item.unlocked_users_known)push('已解锁',`${item.unlocked_users_count??0} 人`)
  const ep=item.episode||{}
  push('分集',[ep.season_num?`S${ep.season_num}`:'',ep.episode_num?`E${ep.episode_num}`:'',ep.end_episode_num?`-E${ep.end_episode_num}`:''].join(''))
  push('频道',item.channel_title)
  push('官方',item.is_official?'官方频道':'')
  push('链接',item.link_type==='share'?'分享链接':item.link_type)
  return rows
}
async function findResources(item:any){
  selected.value=item;resourceDialog.value=true;resourceItems.value=[];resourceError.value='';resourceMessage.value='';resourcePartial.value=false;resourceFilter.value='all'
  await Promise.all([loadTransferTarget(),searchResources()])
}
async function loadTransferTarget(){
  try{const data=await props.api('/transfer/config');const cfg=data.config||{};transferTarget.value={...cfg,target_cid:cfg.explore_cid||cfg.target_cid||'',target_label:cfg.explore_label||cfg.target_label||''};targetError.value='';return transferTarget.value}
  catch(e){transferTarget.value=null;targetError.value=(e as Error).message;return null}
}
function canTransfer(item:any){return item.provider==='115'&&(!!item.share_url||(item.source==='re0'&&item.link_type==='share'&&!!item.slug))}
async function searchResources(){
  if(!selected.value)return
  const version=++resourceRequestVersion
  resourceBusy.value=true;resourceError.value='';resourceItems.value=[];resourcePartial.value=false;resourceTruncated.value=false
  try{const r=await props.api('/explore/resources','POST',{title:selected.value.title,tmdb_id:selected.value.id,media_type:selected.value.kind,year:String(selected.value.year||''),sources:[resourceSource.value]});if(version===resourceRequestVersion){resourceItems.value=(r.items||[]).map(restoreResource);resourcePartial.value=!!r.partial;resourceTruncated.value=!!r.truncated}}
  catch(e){if(version===resourceRequestVersion)resourceError.value=(e as Error).message}finally{if(version===resourceRequestVersion)resourceBusy.value=false}
}
function pointNotice(item:any){
  return item.share_url?'分享链接已就绪，不需要再次获取。':item.is_unlocked?'资源显示为已解锁，实际状态以资源服务返回为准。':item.unlock_points!=null?`预计消耗 ${item.unlock_points} 积分，实际费用以资源服务返回为准。`:'积分费用未知，请先在资源服务中确认。'
}
async function readResourceLink(item:any){
  const r=await props.api('/explore/resources/resolve','POST',{source:'re0',provider:'115',slug:item.slug})
  item.share_url=r.share_url;rememberResource(item)
  return r.share_url as string
}
async function transferResource(item:any){
  if(resourceBusy.value||item.resolveUncertain||item.transferUncertain||item.transferSubmitted)return
  resourceBusy.value=true;resourceError.value=''
  const target=await loadTransferTarget()
  if(!target?.target_cid){resourceBusy.value=false;resourceError.value='请先在「资源入库 → 保存目录」给影视探索设置 115 转存目录。';return}
  const label=target.target_label||`目录 ID ${target.target_cid}`
  const follow=target.auto_organize?'转存核实后自动进入整理。':'转存后需要手动整理或由定时扫描跟进。'
  try{await ElMessageBox.confirm(`${pointNotice(item)}\n目标：${label}（ID ${target.target_cid}）。${follow}\n确认后将获取链接并提交转存。`, '转存到 115', {confirmButtonText:'确认转存',cancelButtonText:'取消',type:'warning'})}catch{resourceBusy.value=false;return}
  resourceError.value='';resourceMessage.value=''
  let stage='resolve'
  try{
    const link=item.share_url||await readResourceLink(item)
    stage='enqueue'
    const result=await props.api('/transfer/jobs','POST',{text:link,target_cid:String(target.target_cid)})
    item.transferSubmitted=true;rememberResource(item)
    const id=result.items?.[0]?.id
    resourceMessage.value=id?`转存任务 #${id} 已入队，目标：${label}。请在任务中心查看进度。`:`转存任务已入队，目标：${label}。请在任务中心查看进度。`
  }catch(e){
    if(stage==='resolve'){item.resolveUncertain=true;rememberResource(item);resourceError.value=(e as Error).message}
    else {item.transferUncertain=true;rememberResource(item);resourceError.value='提交结果未确认，请先在任务中心核查，避免重复转存。 '+(e as Error).message}
  }finally{resourceBusy.value=false}
}
async function shareStrmResource(item:any){
  if(resourceBusy.value||item.resolveUncertain||item.shareSubmitted)return
  // 第二条入库路径：分享直接生成 STRM，播放走分享下载通道，不占自己 115 的空间。
  try{await ElMessageBox.confirm(`${pointNotice(item)}\n会把该分享直接接入本项目的分享库并生成 STRM（不转存到你的网盘，不占空间），整理后进入媒体库。\n确认入库？`, '分享入库（免转存）', {confirmButtonText:'确认入库',cancelButtonText:'取消',type:'warning'})}catch{return}
  resourceBusy.value=true;resourceError.value='';resourceMessage.value=''
  let stage='resolve'
  try{
    const link=item.share_url||await readResourceLink(item)
    stage='ingest'
    const r=await props.api('/share/ingest','POST',{text:link})
    item.shareSubmitted=true;rememberResource(item)
    resourceMessage.value=`已接入分享库 ${r.video_count} 个文件：新建 ${r.count} 个 STRM`+
      (r.skip_count?`，跳过 ${r.skip_count} 个已存在`:'')+
      (r.job_id?`；整理任务 #${r.job_id} 已入队，可在任务中心查看进度`:'；请到「分享 STRM」点击整理')+'。'
  }catch(e){
    if(stage==='resolve'){item.resolveUncertain=true;rememberResource(item);resourceError.value=(e as Error).message}
    else resourceError.value='分享入库未完成：'+(e as Error).message
  }finally{resourceBusy.value=false}
}
async function copyResource(item:any){
  if(resourceBusy.value||item.resolveUncertain)return
  resourceBusy.value=true
  if(!item.share_url){
    try{await ElMessageBox.confirm(`${pointNotice(item)}\n获取链接可能触发解锁，是否继续？`, '获取分享链接', {confirmButtonText:'确认获取',cancelButtonText:'取消',type:'warning'})}catch{resourceBusy.value=false;return}
  }
  resourceError.value='';resourceMessage.value=''
  try{
    let link=item.share_url
    if(!link){try{link=await readResourceLink(item)}catch(e){item.resolveUncertain=true;rememberResource(item);throw e}}
    if(navigator.clipboard&&window.isSecureContext)await navigator.clipboard.writeText(link)
    else {const input=document.createElement('textarea');input.value=link;input.style.position='fixed';input.style.opacity='0';document.body.appendChild(input);input.select();const ok=document.execCommand('copy');input.remove();if(!ok)throw new Error('浏览器不支持复制，请在安全连接下重试。')}
    resourceMessage.value='分享链接已复制。'
  }catch(e){resourceError.value=(e as Error).message}finally{resourceBusy.value=false}
}
const perPage=window.innerWidth<640?30:45
const poster=(p:string)=>p?`https://image.tmdb.org/t/p/w500${p}`:''
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
onMounted(async()=>{await loadGenres();await load()})
</script>
<template>
<div class="page-heading"><div><h1>影视探索</h1><p class="muted">TMDB 数据浏览，入库状态显示在海报下方。</p></div>
<div class="kind-switch" aria-label="作品类型"><button :class="{active:kind==='movie'}" :aria-pressed="kind==='movie'" @click="pick('movie')">电影</button><button :class="{active:kind==='tv'}" :aria-pressed="kind==='tv'" @click="pick('tv')">剧集</button></div></div>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>

<div class="filter-bar">
<div class="search-row">
<input class="search-input" v-model="searchInput" aria-label="搜索电影或剧集" placeholder="搜索电影或剧集名称，如「三体」「Oppenheimer」…" @keydown.enter="doSearch"/>
<el-button v-if="activeQuery" @click="clearSearch" style="flex-shrink:0">取消搜索</el-button>
<el-button type="primary" @click="doSearch" style="flex-shrink:0">搜索</el-button>
</div>

<button class="mobile-filter-toggle" type="button" :aria-expanded="filtersOpen" aria-controls="explore-filters" @click="filtersOpen=!filtersOpen">{{filtersOpen?'收起筛选':'展开筛选'}}<span>{{filterCount?`已选 ${filterCount} 项`:'排序、类型、地区、年份'}}</span><span aria-hidden="true">{{filtersOpen?'−':'＋'}}</span></button>
<div id="explore-filters" class="filter-block" :class="{'mobile-collapsed':!filtersOpen}">
  <div class="frow"><span class="flabel">排序</span>
    <div class="chips"><button v-for="s in sortOptions" :key="s.v" :class="['chip',{active:sort===s.v}]" @click="setSort(s.v)">{{s.t}}</button></div>
  </div>
  <div class="frow"><span class="flabel">类型</span>
    <div class="chips">
      <button :class="['chip',{active:!genre}]" @click="setGenre(undefined)">全部</button>
      <button v-for="g in genres" :key="g.id" :class="['chip',{active:genre===g.id}]" @click="setGenre(g.id)">{{g.name}}</button>
    </div>
  </div>
  <div class="frow"><span class="flabel">地区</span><div class="chips"><button :class="['chip',{active:!region}]" @click="setRegion('')">全部</button><button v-for="r in regionOptions" :key="r[0]" :class="['chip',{active:region===r[0]}]" @click="setRegion(r[0])">{{r[1]}}</button></div></div>
  <div class="frow"><span class="flabel">年份</span>
    <div class="chips">
      <button :class="['chip',{active:!year}]" @click="setYear(undefined)">全部</button>
      <button v-for="y in yearOptions" :key="y" :class="['chip',{active:year===y}]" @click="setYear(y)">{{y}}</button>
    </div>
    <input class="year-input" v-model="yearInput" aria-label="输入年份" inputmode="numeric" placeholder="或输入年份" @keydown.enter="applyYear" @change="applyYear"/>
  </div>
</div>

<p class="region-hint">上映／播出排序可与地区、类型、年份组合，组合结果按 TMDB 日期范围近似筛选；趋势榜及名称搜索暂不支持组合筛选。</p>
<div class="meta-row">
<span v-if="activeQuery" class="search-tag">🔍 {{activeQuery}} <button class="clear-x" aria-label="清除搜索" @click="clearSearch">×</button></span>
<span class="muted count">{{items.length}} 部 · 第 {{page}}/{{totalPages}} 页</span>
</div>
</div>

<el-skeleton v-if="!items.length&&busy" :rows="6" animated/>
<el-empty v-else-if="!items.length" :description="activeQuery?'没有找到「'+activeQuery+'」的相关结果':'没有结果'"/>
<div v-else class="explore-grid">
<article v-for="it in items" :key="it.kind+it.id" class="explore-card">
<div class="poster-image">
<img v-if="it.poster" :src="poster(it.poster)" loading="lazy" :alt="it.title"/>
<el-icon v-else><Film/></el-icon>
<span class="ov type">{{it.kind==='movie'?'电影':'剧集'}}</span>
<span v-if="it.rating" class="ov rating">⭐ {{it.rating.toFixed(1)}}</span>
<span :class="['ov status',badgeClass[it.badge]||'badge-none']">{{it.badge}}{{it.ratio?' '+it.ratio:''}}</span>
</div>
<div class="explore-info">
<h3 :title="it.title">{{it.title}}</h3>
<p class="muted">{{it.year}} · TMDB</p><el-button size="small" @click="findResources(it)">查找资源</el-button>
</div>
</article>
</div>

<div v-if="totalPages>1" class="pagination">
<span>第 {{page}} / {{totalPages}} 页</span>
<el-button :disabled="page<=1" @click="go(page-1)">上一页</el-button>
<el-button :disabled="page>=totalPages" @click="go(page+1)">下一页</el-button>
</div>

<el-dialog v-model="resourceDialog" class="discovery-dialog" width="min(880px, 96vw)" :show-close="true">
<template #header><div class="resource-heading"><span class="resource-kicker">RESOURCE DISCOVERY</span><h2>{{selected?.title}}</h2><p>{{selected?.kind==='tv'?'剧集':'电影'}} · {{selected?.year||'年份未知'}} · {{selected?.badge||'状态未知'}}</p></div></template>
<div class="resource-toolbar"><div class="resource-source"><span>资源来源</span><el-select v-model="resourceSource" style="width:140px" :disabled="resourceBusy" @change="searchResources"><el-option label="RE0" value="re0"/><el-option label="观影" value="guanying"/><el-option label="SeedHub" value="seedhub"/></el-select></div><div class="resource-toolbar-actions"><el-button :icon="Refresh" :loading="resourceBusy" @click="searchResources">刷新</el-button><el-button :icon="Setting" @click="resourceDialog=false;router.push({name:'settings',query:{section:'resource'}})">系统设置</el-button></div></div>
<el-alert v-if="resourceError" :title="resourceError" type="error" show-icon :closable="false"/>
<el-alert v-if="resourceTruncated" title="结果较多，仅显示前 100 条；可尝试其他来源或搜索条件。" type="info" show-icon :closable="false"/>
<el-alert v-if="resourcePartial" title="部分来源查询失败，结果可能不完整。" type="warning" show-icon :closable="false"/>
<el-alert v-if="resourceMessage" :title="resourceMessage" type="success" show-icon :closable="false"/>
<div class="target-summary"><div><span class="target-caption">STRIMKEEP 转存目标</span><strong>{{transferTarget?.target_label|| (transferTarget?.target_cid ? '115 目录' : '未设置目标目录')}}</strong><small v-if="transferTarget?.target_cid">ID {{transferTarget.target_cid}} · {{transferTarget.auto_organize?'转存后自动整理':'需手动整理或定时扫描'}}</small><small v-else>{{targetError||'请先在链接转存中选择 115 目录'}}</small></div><el-button v-if="!transferTarget?.target_cid" size="small" @click="resourceDialog=false;router.push('/transfer')">去设置</el-button></div>
<div class="resource-summary"><div><strong>{{resourceItems.length}}</strong><span> 个候选资源</span></div><div class="resource-filters"><button :class="{active:resourceFilter==='all'}" @click="resourceFilter='all'">全部</button><button :class="{active:resourceFilter==='115'}" @click="resourceFilter='115'">115 网盘</button><button :class="{active:resourceFilter==='ready'}" @click="resourceFilter='ready'">链接就绪</button></div></div>
<el-skeleton v-if="resourceBusy&&!resourceItems.length" :rows="4" animated/>
<el-empty v-else-if="!visibleResources.length&&!resourceError" :description="resourceItems.length?'当前筛选下没有资源':'暂未找到资源，试试其他来源'"/>
<div v-else class="resource-list"><article v-for="(item,index) in visibleResources" :key="item.slug||index" class="resource-card"><div class="resource-card-top"><div class="resource-labels"><span class="source-badge">{{sourceLabel(item.source)}}</span><span class="provider-badge">{{item.provider_label||item.provider||'网盘未知'}}</span><span v-if="item.validate_status==='valid'" class="valid-badge">有效</span></div><span class="resource-cost" :class="{'is-ready':item.share_url}">{{item.share_url?'链接就绪':item.is_unlocked?'已解锁':item.unlock_points!==null?'预计 '+item.unlock_points+' 积分':'费用未知'}}</span></div><h3>{{item.title||selected?.title||'未命名资源'}}</h3><p v-if="resourceRemark(item)" class="resource-remark" :title="resourceRemark(item)">{{resourceRemark(item)}}</p><div class="resource-meta"><span class="publisher-badge">发布者：{{item.sharer||'未知'}}</span><span v-if="item.size">{{item.size}}</span><span v-if="item.release_group">发布组：{{item.release_group}}</span></div><div class="resource-tags"><span v-for="tag in resourceTags(item)" :key="String(tag)">{{tag}}</span></div><details v-if="resourceDetails(item).length" class="resource-detail"><summary>资源详情</summary><dl><template v-for="row in resourceDetails(item)" :key="row[0]"><dt>{{row[0]}}</dt><dd>{{row[1]}}</dd></template></dl></details><div class="resource-card-actions"><span v-if="item.resolveUncertain" class="resource-note">解锁结果待核查</span><span v-else-if="item.transferUncertain" class="resource-note">转存结果待核查</span><span v-else-if="item.transferSubmitted" class="resource-note">已入队，查看任务中心</span><span v-else-if="item.shareSubmitted" class="resource-note">已入分享库，查看任务中心</span><span v-else-if="item.provider!=='115'" class="resource-note">当前仅支持 115 转存</span><span v-else-if="!canTransfer(item)" class="resource-note">请到来源获取链接</span><span v-else class="resource-note">{{item.share_url?'链接就绪':'转存前确认积分'}}</span><div class="resource-buttons" v-if="canTransfer(item)"><el-button size="small" :disabled="resourceBusy||item.resolveUncertain" @click="copyResource(item)">复制链接</el-button><el-button size="small" :disabled="resourceBusy||item.resolveUncertain||item.shareSubmitted||item.transferSubmitted" @click="shareStrmResource(item)">分享入库</el-button><el-button type="primary" size="small" :disabled="resourceBusy||!transferTarget?.target_cid||item.resolveUncertain||item.transferUncertain||item.transferSubmitted" @click="transferResource(item)">转存到 115</el-button></div></div></article></div>
<p class="resource-footnote">获取链接可能消耗积分；转存和整理状态可在任务中心查看。</p>
</el-dialog>
</template>
<style scoped>
:global(.discovery-dialog){margin:4vh auto !important;max-height:92vh;display:flex;flex-direction:column}:global(.discovery-dialog .el-dialog__body){min-height:0;overflow:auto}

.target-summary{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 13px;border:1px solid var(--el-border-color-lighter);background:var(--el-fill-color-light);border-radius:10px}.target-summary>div{display:flex;align-items:baseline;flex-wrap:wrap;gap:6px 10px}.target-caption{font-size:10px;letter-spacing:.06em;color:var(--el-color-primary);font-weight:700}.target-summary strong{font-size:13px}.target-summary small{font-size:12px;color:var(--el-text-color-secondary)}.publisher-badge{color:var(--el-color-primary);font-weight:600}.resource-buttons{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.resource-buttons :deep(.el-button + .el-button){margin-left:0}.resource-heading{padding-right:32px}.resource-kicker{font-size:10px;letter-spacing:.15em;color:var(--el-color-primary);font-weight:700}.resource-heading h2{font-size:22px;margin:5px 0 4px;line-height:1.3}.resource-heading p{margin:0;color:var(--el-text-color-secondary);font-size:13px}.resource-toolbar,.resource-summary,.resource-card-top,.resource-card-actions{display:flex;align-items:center;justify-content:space-between;gap:12px}.resource-toolbar{padding:4px 0 18px}.resource-source,.resource-toolbar-actions{display:flex;align-items:center;gap:10px}.resource-source>span{font-size:13px;color:var(--el-text-color-secondary)}.resource-summary{padding:17px 0 12px;border-top:1px solid var(--el-border-color-lighter);font-size:13px;color:var(--el-text-color-secondary)}.resource-summary strong{font-size:20px;color:var(--el-text-color-primary)}.resource-filters{display:flex;gap:4px;background:var(--el-fill-color-light);padding:3px;border-radius:9px}.resource-filters button{border:0;background:transparent;color:var(--el-text-color-secondary);font-size:12px;padding:6px 9px;border-radius:7px;cursor:pointer}.resource-filters button.active{background:var(--el-bg-color);color:var(--el-color-primary);box-shadow:0 1px 4px #0002}.resource-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;max-height:52vh;overflow:auto;padding:1px 4px 4px 1px}.resource-card{border:1px solid var(--el-border-color-lighter);background:var(--el-fill-color-blank);border-radius:12px;padding:14px;min-width:0;display:flex;flex-direction:column;gap:10px}.resource-card h3{font-size:14px;line-height:1.5;margin:0;overflow-wrap:anywhere}.resource-labels,.resource-tags,.resource-meta{display:flex;flex-wrap:wrap;gap:6px}.resource-labels span,.resource-tags span{font-size:11px;padding:3px 7px;border-radius:5px;background:var(--el-fill-color-light);color:var(--el-text-color-regular)}.resource-labels .source-badge{color:var(--el-color-primary);background:var(--el-color-primary-light-9);font-weight:700}.resource-labels .valid-badge{color:var(--el-color-success)}.resource-cost{white-space:nowrap;font-size:12px;color:var(--el-color-warning)}.resource-cost.is-ready{color:var(--el-color-success)}.resource-meta{font-size:12px;color:var(--el-text-color-secondary)}.resource-card-actions{margin-top:auto;padding-top:10px;border-top:1px solid var(--el-border-color-lighter)}.resource-note,.resource-footnote{font-size:12px;color:var(--el-text-color-secondary);line-height:1.5}.resource-footnote{margin:14px 0 0}.resource-remark{margin:0;font-size:12px;line-height:1.6;color:var(--el-text-color-regular);overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.resource-detail{font-size:12px;color:var(--el-text-color-secondary)}.resource-detail summary{cursor:pointer;color:var(--el-color-primary);font-weight:600;font-size:12px}.resource-detail dl{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:8px 0 0}.resource-detail dt{color:var(--el-text-color-secondary);white-space:nowrap}.resource-detail dd{margin:0;color:var(--el-text-color-regular);overflow-wrap:anywhere}.badge-unknown{background:rgba(130,108,48,.9)}
@media(max-width:640px){.resource-list{grid-template-columns:1fr;max-height:48vh}.resource-toolbar,.resource-summary,.resource-card-actions{align-items:flex-start;flex-wrap:wrap}.resource-heading h2{font-size:19px}.resource-filters{margin-left:auto}}
.kind-switch{display:flex;gap:0;border:1px solid var(--el-border-color,#dcdfe6);border-radius:8px;overflow:hidden}
.kind-switch button{border:0;background:none;padding:8px 20px;cursor:pointer;font-size:14px;color:var(--el-text-color-regular)}
.kind-switch button.active{background:var(--el-color-primary,#24735f);color:#fff;font-weight:600}
.filter-bar{margin-bottom:18px}
.search-row{display:flex;gap:8px;margin-bottom:14px}
.search-input{flex:1;border:1px solid var(--el-border-color,#dcdfe6);border-radius:10px;padding:9px 14px;font-size:14px;background:var(--el-bg-color,#fff);color:var(--el-text-color-primary);outline:none}
.search-input:focus{border-color:var(--el-color-primary,#24735f)}
.filter-block{background:var(--el-fill-color-light,#f8faf9);border:1px solid var(--el-border-color-lighter,#e4e7ed);border-radius:12px;padding:14px 18px;display:grid;gap:12px}
.frow{display:flex;align-items:flex-start;gap:12px}
.flabel{flex-shrink:0;width:34px;padding-top:6px;font-size:13px;color:var(--el-text-color-secondary)}
.chips{display:flex;gap:8px;flex-wrap:wrap;flex:1}
.chip{border:1px solid var(--el-border-color,#dcdfe6);background:var(--el-bg-color,#fff);border-radius:16px;padding:4px 14px;cursor:pointer;font-size:13px;color:var(--el-text-color-regular);transition:.15s}
.chip:hover{border-color:var(--el-color-primary,#24735f);color:var(--el-color-primary,#24735f)}
.chip.active{background:var(--el-color-primary,#24735f);color:#fff;border-color:var(--el-color-primary,#24735f)}
.year-input{width:110px;border:1px solid var(--el-border-color,#dcdfe6);border-radius:8px;padding:5px 10px;font-size:12px;background:var(--el-bg-color,#fff);color:var(--el-text-color-primary);outline:none;flex-shrink:0}
.year-input:focus{border-color:var(--el-color-primary,#24735f)}
.meta-row{display:flex;align-items:center;gap:10px;margin-top:12px;min-height:24px}
.count{margin-left:auto;font-size:12px}
.search-tag{font-size:12px;color:var(--el-color-primary,#24735f);background:var(--el-color-primary-light-9,#edf5f0);border-radius:4px;padding:2px 8px;display:inline-flex;align-items:center;gap:6px}
.clear-x{border:0;background:none;color:inherit;cursor:pointer;font-size:13px;padding:0;line-height:1}
/* auto-fill：按可用宽度自动排满整行，不留两侧空白 */
.explore-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media(min-width:640px){.explore-grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}}
.explore-card{border-radius:10px;overflow:hidden;background:var(--el-bg-color,#fff);border:1px solid var(--el-border-color-lighter,#e4e7ed);transition:transform .15s}
.explore-card:hover{transform:translateY(-3px);box-shadow:0 4px 16px rgba(0,0,0,.08)}
.poster-image{position:relative;aspect-ratio:2/3;overflow:hidden;background:var(--el-fill-color-light,#f5f7fa)}
.poster-image img{width:100%;height:100%;object-fit:cover;display:block}
.ov{position:absolute;font-size:10px;font-weight:600;padding:2px 8px;border-radius:6px;color:#fff;line-height:1.6;backdrop-filter:blur(2px)}
.ov.type{top:8px;left:8px;background:rgba(15,23,42,.66)}
.ov.rating{top:8px;right:8px;background:rgba(15,23,42,.66);color:#fbbf24}
.ov.status{left:8px;bottom:8px}
.status.badge-ok{background:rgba(34,163,107,.92)}
.status.badge-partial{background:rgba(59,130,196,.92)}
.status.badge-none{background:rgba(107,114,128,.85)}
.explore-info{padding:8px 10px 10px}
.explore-info h3{font-size:13px;font-weight:600;margin:0 0 3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.explore-info .muted{font-size:11px;margin:0}
@media(max-width:640px){
  .frow{flex-wrap:wrap}
  .flabel{width:100%;padding-top:0}
  .year-input{width:100%}
}

.region-hint{margin:10px 0 0;color:var(--muted);font-size:11px;line-height:1.5}
/* Discovery shares the same restrained, poster-led visual language as the overview. */
.page-heading{margin-bottom:24px;gap:20px}.page-heading h1{font-size:27px;letter-spacing:-.6px;margin-bottom:7px}.page-heading p{font-size:13px}
.kind-switch{padding:4px;gap:3px;border:1px solid var(--line);border-radius:12px;background:var(--surface)}.kind-switch button{min-width:70px;padding:8px 16px;border-radius:8px;font-size:13px}.kind-switch button.active{background:var(--accent)}
.search-row{margin-bottom:16px;gap:10px}.search-input{min-width:0;height:44px;box-sizing:border-box;padding:0 16px;border-color:var(--line);background:var(--surface);box-shadow:var(--panel-shadow)}.search-row :deep(.el-button){height:44px;padding:0 22px;border-radius:10px}
.filter-block{background:var(--surface);border-color:var(--line);padding:17px 20px;gap:14px;box-shadow:var(--panel-shadow)}.flabel{font-size:12px;font-weight:600;color:var(--muted)}.chips{gap:6px}.chip{border:1px solid transparent;background:var(--surface-soft);color:var(--el-text-color-regular);padding:6px 12px;font-size:12px;border-radius:8px}.chip.active{background:var(--accent);border-color:var(--accent);color:#fff}.year-input{height:30px;box-sizing:border-box;border-color:var(--line)}
.meta-row{margin:16px 0 4px}.explore-grid{gap:18px}.explore-card{border:1px solid var(--line);border-radius:12px;background:var(--surface);box-shadow:var(--panel-shadow)}.explore-card:hover{box-shadow:0 10px 28px rgba(28,58,105,.12)}.poster-image{border-radius:11px 11px 0 0}.ov{padding:4px 8px;font-size:10px}.explore-info{padding:12px}.explore-info h3{font-size:14px;color:var(--text);margin-bottom:5px}.explore-info .muted{font-size:11px}.explore-info :deep(.el-button){margin-top:10px;width:100%;border-color:var(--line);background:var(--surface-soft);color:var(--accent);font-weight:600;border-radius:8px}
@media(max-width:640px){.page-heading{margin-bottom:18px}.page-heading h1{font-size:24px}.kind-switch button{min-width:54px;padding:7px 10px}.filter-block{padding:14px;gap:13px}.frow{gap:7px}.chips{gap:6px}.chip{padding:7px 10px}.year-input{width:110px}.explore-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.explore-info{padding:8px}.explore-info h3{font-size:12px}.explore-info :deep(.el-button){padding:0 5px;font-size:11px}.search-row :deep(.el-button){padding:0 13px}}
/* Poster-led discovery layout; all existing controls and filters are retained. */
.page-heading{align-items:center}.page-heading h1{font-size:25px}.kind-switch{border-radius:9px}.kind-switch button{border-radius:6px}
.search-input{border-radius:8px}.search-row :deep(.el-button){border-radius:8px}.filter-block{border-radius:10px;padding:15px 18px;gap:11px}.frow{align-items:center}.flabel{padding-top:0}.chip{border-radius:7px;padding:6px 11px}.chip:hover:not(.active){background:color-mix(in srgb,var(--accent) 9%,var(--surface));border-color:transparent}
.explore-grid{gap:14px}.explore-card{border-radius:9px;transition:transform .16s,box-shadow .16s}.poster-image{border-radius:8px 8px 0 0}.explore-info{padding:10px}.explore-info h3{line-height:1.45}.explore-info :deep(.el-button){height:30px;margin-top:9px;border-radius:6px}
.resource-heading h2{letter-spacing:-.35px}.target-summary{border-radius:8px;background:color-mix(in srgb,var(--accent) 5%,var(--surface));border-color:var(--line)}.resource-card{border-color:var(--line);border-radius:9px;padding:16px;box-shadow:0 3px 12px rgba(32,55,85,.025)}.resource-card h3{font-size:15px}.publisher-badge{padding:3px 7px;background:var(--el-color-primary-light-9);border-radius:5px}.resource-card-actions{gap:8px}.resource-buttons :deep(.el-button){border-radius:7px}
@media(max-width:640px){.page-heading h1{font-size:23px}.filter-block{padding:13px;gap:12px}.frow{align-items:flex-start}.chip{padding:6px 9px}.explore-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.explore-info{padding:8px 7px}.explore-info h3{font-size:12px}.resource-card{padding:13px}.resource-toolbar-actions{flex-wrap:wrap}.resource-buttons{width:100%;justify-content:stretch}.resource-buttons :deep(.el-button){flex:1;min-width:0}}
/* Keep a single scrolling surface in the resource dialog. */
.resource-list{max-height:none;overflow:visible;padding:1px}
.resource-card-actions{align-items:center;flex-wrap:wrap}
.resource-buttons{margin-left:auto}
.resource-buttons :deep(.el-button){min-height:34px;padding-inline:12px}
@media(max-width:640px){.resource-list{max-height:none;overflow:visible}.resource-card-actions{align-items:stretch}.resource-note{width:100%}.resource-buttons{display:flex;width:100%;margin-left:0}.resource-buttons :deep(.el-button){flex:1;min-height:40px;padding-inline:8px}}
.mobile-filter-toggle{display:none}
.search-input::placeholder,.year-input::placeholder{color:var(--muted);opacity:1}
@media(max-width:640px){.mobile-filter-toggle{display:flex;align-items:center;gap:8px;width:100%;padding:10px 11px;margin:0 0 10px;border:1px solid var(--line);border-radius:8px;background:var(--surface-soft);color:var(--text);font-size:13px;font-weight:600;text-align:left}.mobile-filter-toggle span:first-child{flex:1;color:var(--muted);font-size:11px;font-weight:400}.mobile-filter-toggle span:last-child{color:var(--accent);font-size:17px}.filter-block.mobile-collapsed{display:none}}
@media(max-width:640px){.explore-info h3{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.8em;overflow-wrap:anywhere}}
</style>
