<script setup lang="ts">
import {useVisiblePolling} from './pageActivity'
import {onMounted,shallowRef,ref,computed} from 'vue'
import {VideoPlay,Refresh,Film,Collection,Cloudy,List,Link,Grid,Finished} from '@element-plus/icons-vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>();defineEmits<{navigate:[page:string]}>()
const data=shallowRef<any>(null),error=ref(''),busy=ref(false),broken=ref<Record<string,boolean>>({})
const recentKind=ref<'all'|'Movie'|'Series'>('all'),showAllRecent=ref(false)
const posterUrl=(item:any)=>item.image_id&&!broken.value[item.image_id]?`/api/v1/dashboard/image/${item.image_id}`:item.tmdb_id?`/api/v1/dashboard/tmdb-poster/${item.kind==='Series'?'tv':'movie'}/${item.tmdb_id}`:''
const posterFailed=ref<Record<string,boolean>>({})
function posterError(item:any){if(item.image_id&&!broken.value[item.image_id])broken.value={...broken.value,[item.image_id]:true};else posterFailed.value={...posterFailed.value,[item.id]:true}}
const recentList=computed(()=>{const items=(data.value?.emby?.recent||[]).filter((item:any)=>recentKind.value==='all'||item.kind===recentKind.value);return showAllRecent.value?items:items.slice(0,6)})
const cloud=shallowRef<any>(null),tasks=shallowRef<any>(null),schedules=shallowRef<any>(null),syncProfiles=shallowRef<any>(null),events=shallowRef<any>(null),channels=shallowRef<any>(null)
async function loadAux(){const paths=['/tasks','/system/schedules','/sync/profiles','/events?limit=5','/channels'];const values=[tasks,schedules,syncProfiles,events,channels];const result=await Promise.allSettled(paths.map(p=>props.api(p)));result.forEach((v,i)=>{values[i].value=v.status==='fulfilled'?v.value:null})}
const syncScheduleStatus=computed(()=>syncProfiles.value?`${(syncProfiles.value.items||[]).filter((p:any)=>p.enabled).length} 个定时任务开启`:'状态不可用')
async function refreshAll(){await Promise.all([load(),loadAux(),loadCloud()])}
async function loadCloud(){try{cloud.value=await props.api('/cloud/account')}catch{cloud.value=null}}
const bytes=(n:any)=>{if(typeof n!=='number'||!Number.isFinite(n))return '—';if(n>=1024**4)return (n/1024**4).toFixed(2)+' TB';return (n/1024**3).toFixed(1)+' GB'}
const cloudPercent=computed(()=>cloud.value?.space_total>0?Math.min(100,Math.round(cloud.value.space_used/cloud.value.space_total*100)):null)
const recentTasks=computed(()=>[...(tasks.value?.active||[]),...(tasks.value?.history||[])].slice(0,4))
const jobName=(kind:string)=>({organize:'影视整理',transfer:'链接转存',share_organize:'分享整理',strm_sync:'STRM 同步',archive_index:'媒体索引'} as Record<string,string>)[kind]||kind||'后台任务'
const jobState=(state:string)=>({queued:'排队中',running:'进行中',paused:'已暂停',waiting_login:'等待登录',needs_review:'待处理',cancelled:'已取消',completed:'已完成',partial:'部分完成'} as Record<string,string>)[state]||state

async function load(){if(busy.value)return;busy.value=true;try{data.value=await props.api('/dashboard');error.value=''}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function historyName(name:string){return (name||'').replace(/\bis playing\b/i,'开始播放')}
function fmt(ts?:number){if(!ts)return '—';const d=new Date(ts*1000);const p=(n:number)=>String(n).padStart(2,'0');return `${d.getMonth()+1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`}
const gb=(n?:number)=>n==null?'—':(n/1024**3).toFixed(0)
const diskUsed=computed(()=>{const t=data.value?.system?.total_bytes;if(!t)return '—';return Math.round(data.value.system.used_bytes/t*100)+'%'})
const trendBars=computed(()=>{
  const rows:any[]=data.value?.emby?.trend||[]
  const max=Math.max(1,...rows.map((r:any)=>Number(r.count)||0))
  return rows.map((r:any,i:number)=>{const h=Math.max(3,Math.round((Number(r.count)||0)/max*100));return {date:r.date,count:Number(r.count)||0,x:12+i*42,y:132-h,h}})
})
const trendTotal=computed(()=>(data.value?.emby?.trend||[]).reduce((sum:number,r:any)=>sum+(Number(r.count)||0),0))
const hero=computed(()=>[
  {icon:Film,label:'电影',value:data.value?.emby?.counts?.MovieCount,sub:'Emby 媒体库'},
  {icon:Collection,label:'剧集',value:data.value?.emby?.counts?.SeriesCount,sub:'Emby 媒体库'},
  {icon:Cloudy,label:'115 网盘',value:bytes(cloud.value?.space_used),sub:cloud.value?.space_total?`总容量 ${bytes(cloud.value.space_total)}`:'容量暂不可用'},
  {icon:List,label:'后台任务',value:data.value?.system?.active_tasks,sub:'正在执行或排队'},
])
const strip=computed(()=>[
  {k:'后台任务',v:String(data.value?.system?.active_tasks??0)},
  {k:'最近任务',v:fmt(data.value?.stats?.last_at)},
  {k:'调度器',v:data.value?.system?.scheduler_ok?'正常':'异常',warn:!data.value?.system?.scheduler_ok},
  {k:'115',v:data.value?.system?.['115_configured']?'已配置':'未配置'},
  {k:'TMDB',v:data.value?.system?.tmdb_configured?'已配置':'未配置'},
  {k:'Emby',v:data.value?.emby?.connected?'已连接':data.value?.emby?.configured?'连接暂不可用':'未配置',warn:!data.value?.emby?.connected},
])
onMounted(()=>{void load();void loadAux();void loadCloud()});useVisiblePolling(load,20000);useVisiblePolling(loadAux,60000);useVisiblePolling(loadCloud,600000)
</script>
<template><div class="page-title"><div><h1>媒体总览</h1><p class="muted">媒体库规模、整理进度与播放动态。</p></div><el-button :icon="Refresh" :loading="busy" @click="refreshAll">刷新</el-button></div>
<el-alert v-if="error" :title="error" type="warning"/>
<el-skeleton v-if="!data&&busy" :rows="8" animated/>
<template v-if="data">
<el-alert v-if="data.emby.error" :title="data.emby.error" type="warning" :closable="false" class="global-error"/>
<p v-if="data.emby.stale" class="hint stale-hint">当前显示上次成功读取的 Emby 数据，非实时数据。</p>

<section v-if="data.emby.recent?.length" class="overview-feature" aria-label="最近入库推荐">
  <img v-if="posterUrl(data.emby.recent[0])&&!posterFailed[data.emby.recent[0].id]" class="overview-feature-art" :src="posterUrl(data.emby.recent[0])" alt="" @error="posterError(data.emby.recent[0])"/>
  <div class="overview-feature-copy"><span class="overview-feature-eyebrow">最近入库 · YOUR MEDIA LIBRARY</span><h2>{{data.emby.recent[0].name}}</h2><p>{{data.emby.recent[0].kind==='Series'?'剧集':'电影'}}<template v-if="data.emby.recent[0].year"> · {{data.emby.recent[0].year}}</template><template v-if="data.emby.recent[0].episode"> · {{data.emby.recent[0].episode}}</template></p><button @click="$emit('navigate','explore')">探索更多影视 <span aria-hidden="true">→</span></button></div>
</section>

<div class="hero-stats">
  <article v-for="h in hero" :key="h.label" class="hero-card">
    <div class="hero-heading"><el-icon><component :is="h.icon"/></el-icon><span>{{h.label}}</span></div>
    <strong class="hero-value">{{h.value??'—'}}<small v-if="h.label==='电影'||h.label==='剧集'"> 部</small></strong><small class="hero-sub">{{h.sub}}</small>
    <div v-if="h.label==='115 网盘'&&cloudPercent!==null" class="usage-track metric-track"><span :style="{width:cloudPercent+'%'}"/></div>
  </article>
</div>



<section class="panel recent-panel">
  <div class="section-title recent-heading"><div class="recent-heading-main"><el-icon class="section-icon"><Grid/></el-icon><h2>最近添加</h2><div class="recent-tabs" aria-label="筛选最近添加"><button :class="{selected:recentKind==='all'}" @click="recentKind='all';showAllRecent=false">全部</button><button :class="{selected:recentKind==='Movie'}" @click="recentKind='Movie';showAllRecent=false">电影</button><button :class="{selected:recentKind==='Series'}" @click="recentKind='Series';showAllRecent=false">剧集</button></div></div><button v-if="(data.emby.recent||[]).filter((item:any)=>recentKind==='all'||item.kind===recentKind).length>6" class="text-link" @click="showAllRecent=!showAllRecent">{{showAllRecent?'收起':'显示更多'}} ›</button></div>
  <div v-if="recentList.length" class="poster-grid" :class="{expanded:showAllRecent}"><article v-for="item in recentList" :key="item.id" class="poster-card"><div class="poster-image"><img v-if="posterUrl(item)&&!posterFailed[item.id]" :src="posterUrl(item)" loading="lazy" :alt="item.name" @error="posterError(item)"/><div v-else class="poster-placeholder"><el-icon><Film/></el-icon><span>暂无海报</span></div><span class="poster-type">{{item.kind==='Series'?'剧集':'电影'}}</span></div><h3>{{item.name}}</h3><p class="muted poster-description">{{item.episode||item.year||'Emby 媒体库'}}</p><span class="library-badge">已入库</span></article></div>
  <el-empty v-else :description="data.emby.connected?'暂无对应的最近入库内容':'连接 Emby 后显示最近入库'" :image-size="65"/>
</section>
<div class="overview-duo">
<section class="panel overview-cloud"><div class="section-title"><div class="section-heading"><el-icon class="section-icon"><Cloudy/></el-icon><h2>115 网盘连接</h2></div><el-button text @click="$emit('navigate','account')">查看网盘 →</el-button></div><template v-if="cloud&&!cloud.error&&cloud.space_total"><p class="cloud-number">{{bytes(cloud.space_used)}} <small>/ {{bytes(cloud.space_total)}}</small></p><div class="usage-track"><span :style="{width:cloudPercent+'%'}"/></div><div class="cloud-details"><div><span>账号</span><b>{{cloud.nickname||'115 网盘'}}</b></div><div><span>总空间</span><b>{{bytes(cloud.space_total)}}</b></div><div><span>已使用</span><b>{{bytes(cloud.space_used)}} · {{cloudPercent}}%</b></div><div><span>剩余空间</span><b>{{bytes(Math.max(0,cloud.space_total-cloud.space_used))}}</b></div></div></template><p v-else class="muted">{{data.system['115_configured']?'网盘容量暂不可用，请到网盘页面查看':'尚未配置 115 网盘'}}</p></section>
<section class="panel overview-tasks"><div class="section-title"><div class="section-heading"><el-icon class="section-icon"><Finished/></el-icon><h2>最近任务</h2></div><el-button text @click="$emit('navigate','tasks')">查看全部 →</el-button></div><div v-if="recentTasks.length" class="overview-task-list"><div v-for="job in recentTasks" :key="job.id" class="overview-task"><div class="task-icon"><el-icon><Finished/></el-icon></div><div class="task-info"><strong>{{jobName(job.kind)}} <small>#{{job.id}}</small></strong><p class="muted">{{job.stage||jobState(job.state)}} · {{fmt(job.updated)}}</p></div><div v-if="job.total>0" class="task-progress"><div class="usage-track"><span :style="{width:Math.min(100,Math.max(0,job.progress))+'%'}"/></div><small>{{job.progress}}%</small></div><span class="task-status">{{jobState(job.state)}}</span></div></div><p v-else class="muted">{{tasks?'暂无任务':'任务信息暂不可用'}}</p></section>
</div>
<div class="status-strip">
  <span v-for="s in strip" :key="s.k" :class="['strip-item',{warn:s.warn}]"><em>{{s.k}}</em>{{s.v}}</span>
  <span class="strip-item"><em>磁盘</em>{{gb(data.system.used_bytes)}}/{{gb(data.system.total_bytes)}} GB · {{diskUsed}}</span>
</div>
<div class="overview-duo">
<section class="panel"><div class="section-title"><h2>自动化状态</h2><el-button text @click="$emit('navigate','sync')">管理同步任务 →</el-button></div><div class="automation-lines"><div><span>调度器</span><b>{{data.system.scheduler_ok?'运行正常':'状态异常'}}</b></div><div><span>自动整理</span><b>{{schedules?(schedules.config.scan_enabled?'已启用':'已停用'):'状态不可用'}}</b></div><div><span>STRM 同步</span><b>{{syncScheduleStatus}}</b></div><div><span>频道监控</span><b>{{channels?(channels.config?.enabled?'已启用':'已停用'):'状态不可用'}}</b></div></div></section>
<section class="panel"><div class="section-title"><h2>最近动态</h2><el-button text @click="$emit('navigate','logs')">实时日志 →</el-button></div><div v-if="events?.items?.length" class="activity-list"><div v-for="event in events.items" :key="event.id"><span>{{event.message}}</span><time>{{fmt(event.at)}}</time></div></div><p v-else class="muted">{{events?'暂无动态':'动态信息暂不可用'}}</p></section>
</div>
<div class="duo">
<section class="panel half">
  <div class="section-title"><h2>任务概览</h2><el-button text @click="$emit('navigate','tasks')">任务中心 →</el-button></div>
  <div class="big-three">
    <div><strong>{{data.stats.today}}</strong><span>今日</span></div>
    <div><strong>{{data.stats.week}}</strong><span>近 7 天</span></div>
    <div><strong>{{data.stats.total}}</strong><span>累计</span></div>
  </div>
  <div class="stat-lines">
    <div class="line"><span>成功</span><b>{{data.stats.completed}}</b></div>
    <div class="line"><span>待复核</span><b>{{data.stats.partial}}</b></div>
    <div class="line"><span>排队或执行中</span><b>{{data.stats.active}}</b></div>
    <div class="line"><span>已登记网盘文件</span><b>{{data.stats.files}}</b></div>
    <div class="line"><span>本地整理库</span><b>{{data.local.movies+data.local.series}} <small>电影 {{data.local.movies}} · 剧集 {{data.local.series}} · 单集 {{data.local.episodes}}</small></b></div>
    <div class="line"><span>最近任务更新</span><b>{{fmt(data.stats.last_at)}}</b></div>
  </div>
</section>
<section class="panel half">
  <div class="section-title"><h2>近 {{data.emby.trend_days||14}} 天观看</h2><span class="muted">共 {{trendTotal}} 次播放</span></div>
  <svg v-if="data.emby.trend.length" viewBox="0 0 620 160" class="trend-chart" role="img" aria-label="近 14 天播放次数">
    <defs><linearGradient id="barGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="var(--el-color-primary)" stop-opacity=".9"/>
      <stop offset="1" stop-color="var(--el-color-primary)" stop-opacity=".45"/>
    </linearGradient></defs>
    <line x1="6" y1="132" x2="614" y2="132" stroke="currentColor" stroke-opacity=".15"/>
    <g v-for="bar in trendBars" :key="bar.date">
      <rect :x="bar.x" :y="bar.count?bar.y:128" width="26" :height="bar.count?bar.h:3" rx="5" :fill="bar.count?'url(#barGrad)':'currentColor'" :opacity="bar.count?.9:.14"><title>{{bar.date}} · {{bar.count}} 次</title></rect>
      <text v-if="bar.count" :x="bar.x+13" :y="bar.y-6" text-anchor="middle" font-size="10" font-weight="600" fill="currentColor" fill-opacity=".75">{{bar.count}}</text>
      <text :x="bar.x+13" y="150" text-anchor="middle" font-size="9" fill="currentColor" fill-opacity=".5">{{bar.date}}</text>
    </g>
  </svg>
  <el-empty v-else description="还没有播放数据" :image-size="60"/>
</section>
</div>

<section class="panel"><div class="section-title"><h2>观看记录</h2><span class="muted">{{data.emby.history.length?'最近 '+data.emby.history.length+' 次':'—'}}</span></div><div v-if="data.emby.history.length" class="history-list"><div v-for="(h,i) in data.emby.history" :key="i" class="history-row"><span class="history-time">{{fmt(h.at)}}</span><span class="wrap">{{historyName(h.name)}}</span></div></div><el-empty v-else description="Emby 还没有播放记录" :image-size="60"/></section>

<section class="panel">
  <div class="section-title"><h2>正在观看</h2><el-tag :type="data.emby.connected?'success':'info'">{{data.emby.connected?'Emby 已连接':'Emby 未连接'}}</el-tag></div>
  <div v-if="data.emby.playing.length" class="watching-grid"><article v-for="(s,i) in data.emby.playing" :key="i" class="watching-card"><el-icon><VideoPlay/></el-icon><div><h3>{{s.series? s.series+' · ':''}}{{s.name}}</h3><p class="muted">{{s.user}} · {{s.client}} · {{s.paused?'已暂停':'播放中'}}</p></div></article></div>
  <div v-else class="idle-box"><el-icon><VideoPlay/></el-icon><p>{{data.emby.connected?'当前没有正在播放的内容':'配置 Emby 后显示正在观看'}}</p></div>
</section>
</template></template>

<style scoped>
.overview-feature{position:relative;isolation:isolate;overflow:hidden;min-height:225px;display:flex;align-items:center;margin:0 0 16px;border-radius:10px;background:#14243c;color:#fff}
.overview-feature::after{content:'';position:absolute;inset:0;z-index:-1;background:linear-gradient(90deg,#102035 0%,rgba(16,32,53,.96) 32%,rgba(16,32,53,.45) 70%,rgba(16,32,53,.2) 100%)}
.overview-feature-art{position:absolute;z-index:-2;inset:0 0 0 auto;width:70%;height:100%;object-fit:cover;object-position:center 32%;filter:saturate(.86)}
.overview-feature-copy{padding:30px 34px;max-width:70%}.overview-feature-eyebrow{font-size:10px;letter-spacing:.14em;color:#b7cbe7}.overview-feature h2{margin:12px 0 8px;font-size:clamp(23px,2.5vw,34px);line-height:1.25;color:#fff}.overview-feature p{font-size:12px;color:#d0d9e7;margin:0 0 20px}.overview-feature button{border:0;border-radius:7px;padding:10px 15px;background:#236cf2;color:#fff;font-size:12px;font-weight:600}.overview-feature button span{margin-left:10px}.overview-feature button:focus-visible{outline:2px solid white;outline-offset:3px}
@media(max-width:760px){.overview-feature{min-height:165px;margin-bottom:12px}.overview-feature-copy{padding:22px 20px;max-width:78%}.overview-feature h2{font-size:22px}.overview-feature p{margin-bottom:14px}.overview-feature-art{width:77%}}
.stale-hint{margin:0 0 14px}
.hero-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px}
.hero-card{display:flex;align-items:center;gap:14px;background:linear-gradient(120deg,var(--el-color-primary-light-9,var(--surface)),var(--surface) 70%);border:1px solid var(--el-border-color-lighter,#e4e7ed);border-radius:16px;padding:18px 20px}
.hero-icon{width:46px;height:46px;border-radius:13px;background:var(--el-color-primary-light-9);color:var(--el-color-primary);display:grid;place-items:center;font-size:22px;flex-shrink:0}
.hero-copy{display:grid;gap:2px;min-width:0}
.hero-copy span{font-size:12px;color:var(--el-text-color-secondary)}
.hero-copy strong{font-size:28px;font-weight:700;letter-spacing:-.5px;line-height:1.1;color:var(--el-text-color-primary)}
.hero-copy small{font-size:11px;color:var(--el-text-color-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.status-strip{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.strip-item{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--el-text-color-regular);background:var(--surface,var(--el-bg-color));border:1px solid var(--el-border-color-lighter,#e4e7ed);border-radius:20px;padding:4px 12px}
.strip-item em{font-style:normal;color:var(--el-text-color-secondary)}
.strip-item.warn{border-color:var(--el-color-warning);color:var(--el-color-warning)}
.duo{display:grid;grid-template-columns:5fr 7fr;gap:16px;align-items:stretch;margin-bottom:16px}
.duo .half{margin-bottom:0}
.big-three{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
.big-three>div{background:var(--el-fill-color-light,#f6f8fc);border-radius:10px;padding:13px 15px;display:grid;gap:2px}
.big-three strong{font-size:22px;font-weight:700;color:var(--el-text-color-primary);line-height:1.1}
.big-three span{font-size:11px;color:var(--el-text-color-secondary)}
.stat-lines{display:grid}
.stat-lines .line{display:flex;justify-content:space-between;align-items:baseline;gap:14px;padding:8px 2px;border-top:1px solid var(--el-border-color-lighter,#e6ebf2);font-size:12.5px}
.stat-lines .line:first-child{border-top:0}
.stat-lines span{color:var(--el-text-color-secondary)}
.stat-lines b{font-weight:600;color:var(--el-text-color-primary);text-align:right}
.stat-lines b small{font-weight:400;color:var(--el-text-color-secondary);font-size:11px}
.trend-chart{width:100%;height:170px;color:var(--el-text-color-primary,#182338)}
.history-list{display:grid}
.history-row{display:grid;grid-template-columns:auto 1fr;gap:12px;padding:9px 0;border-bottom:1px solid var(--el-border-color-lighter,#e6ebf2);font-size:13px;line-height:1.6}
.history-row:last-child{border-bottom:0}
.history-time{color:var(--el-text-color-secondary,#8892a6);font-size:12px;white-space:nowrap}
.idle-box{display:grid;place-items:center;gap:8px;min-height:170px;color:var(--el-text-color-secondary);border:1px dashed var(--el-border-color-lighter,#e4e7ed);border-radius:12px;font-size:12px}
.idle-box .el-icon{font-size:30px;opacity:.5}
.idle-box p{margin:0}
@media(max-width:900px){
  .hero-stats{grid-template-columns:repeat(2,1fr)}
  .duo{grid-template-columns:1fr}
}
@media(max-width:560px){.hero-stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.hero-card{padding:14px 12px;gap:9px}.hero-icon{width:34px;height:34px;font-size:17px}.hero-copy strong{font-size:20px}}
.overview-duo{display:grid;grid-template-columns:5fr 7fr;gap:16px}.overview-duo>.panel{min-width:0}.cloud-number{font-size:25px;font-weight:700;margin:4px 0 12px}.cloud-number small{font-size:13px;color:var(--muted);font-weight:400}.usage-track{height:7px;border-radius:7px;background:var(--el-fill-color-light);overflow:hidden}.usage-track span{display:block;height:100%;background:var(--accent);border-radius:7px}.overview-task,.automation-lines>div,.activity-list>div{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line);font-size:12px}.overview-task:last-child,.automation-lines>div:last-child,.activity-list>div:last-child{border:0}.overview-task strong{font-size:13px}.overview-task small,.overview-task p,.activity-list time{color:var(--muted);font-size:11px}.overview-task p{margin:4px 0 0}.overview-task>span{color:var(--accent);white-space:nowrap}.automation-lines span{color:var(--muted)}.activity-list span{min-width:0;overflow-wrap:anywhere}.activity-list time{white-space:nowrap}@media(max-width:900px){.overview-duo{grid-template-columns:1fr;gap:0}}

/* Overview composition */
.hero-stats{gap:14px;margin-bottom:16px}
.hero-card{display:block;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:19px 20px;min-width:0;box-shadow:var(--panel-shadow)}
.hero-heading{display:flex;align-items:center;gap:10px;font-size:13px;font-weight:650;color:var(--text);margin-bottom:14px}.hero-heading>.el-icon{font-size:21px;color:var(--accent)}.hero-heading .hero-arrow{margin-left:auto;font-size:14px;color:var(--muted)}
.hero-value{display:block;font-size:26px;line-height:1.15;color:var(--text);letter-spacing:-.5px}.hero-value small{font-size:12px;font-weight:400;color:var(--muted);letter-spacing:0}.hero-sub{display:block;margin-top:8px;color:var(--muted);font-size:12px}.metric-track{margin-top:9px}
.recent-panel{padding:20px!important}.recent-heading-main,.section-heading{display:flex;align-items:center;gap:10px}.section-icon{font-size:21px;color:var(--accent)}.recent-heading h2,.section-heading h2{font-size:17px}.recent-tabs{display:flex;gap:4px;margin-left:12px}.recent-tabs button{border:0;border-radius:20px;padding:6px 14px;background:var(--surface-soft);color:var(--muted);font-size:12px;cursor:pointer}.recent-tabs button.selected{background:var(--accent);color:white}.text-link{background:none;border:0;color:var(--muted);cursor:pointer;font-size:12px;white-space:nowrap}
.poster-grid{gap:14px}.poster-image{position:relative}.poster-type{position:absolute;top:7px;left:7px;background:rgba(20,31,49,.76);color:white;padding:3px 7px;border-radius:5px;font-size:10px}.poster-placeholder{height:100%;width:100%;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:8px;background:linear-gradient(150deg,var(--surface-soft),color-mix(in srgb,var(--accent) 9%,var(--surface)));color:var(--muted);font-size:12px}.poster-placeholder .el-icon{font-size:32px;opacity:.65}.poster-card h3{margin:9px 0 3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.poster-description{margin:0 0 7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px}.library-badge{display:inline-block;border-radius:15px;background:color-mix(in srgb,var(--accent) 11%,var(--surface));color:var(--accent);padding:3px 9px;font-size:10px}
.cloud-details{margin-top:14px;border-top:1px solid var(--line)}.cloud-details>div{display:flex;justify-content:space-between;gap:10px;padding-top:10px;font-size:12px}.cloud-details span{color:var(--muted)}.cloud-details b{color:var(--text);font-weight:600}.overview-task{gap:12px}.task-icon{width:35px;height:35px;flex:none;display:grid;place-items:center;border-radius:10px;background:var(--el-color-primary-light-9);color:var(--accent);font-size:18px}.task-info{min-width:0;flex:1}.task-info p{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.task-progress{width:28%;display:flex;gap:7px;align-items:center}.task-progress .usage-track{flex:1}.task-progress small{color:var(--muted);font-size:11px}.overview-task .task-status{font-size:11px}.status-strip{margin-top:2px}
@media(max-width:760px){.hero-card{padding:15px 14px;border-radius:11px}.hero-heading{margin-bottom:10px;font-size:12px}.hero-heading>.el-icon{font-size:18px}.hero-value{font-size:21px}.hero-sub{font-size:11px}.recent-panel{padding:14px!important}.recent-heading{gap:5px}.recent-heading-main{gap:6px}.recent-heading h2,.section-heading h2{font-size:15px}.recent-tabs{margin-left:4px;gap:2px}.recent-tabs button{padding:5px 8px;font-size:11px}.poster-grid{gap:12px}.task-progress{width:25%}}
/* A compact shelf on phones; “查看全部” returns to a full grid. */
@media(max-width:760px){.poster-grid:not(.expanded){grid-template-columns:none;grid-auto-flow:column;grid-auto-columns:minmax(105px,31%);overflow-x:auto;overscroll-behavior-inline:contain;scroll-snap-type:x proximity;padding-bottom:8px}.poster-grid:not(.expanded) .poster-card{scroll-snap-align:start}.overview-task{flex-wrap:wrap}.task-info{flex:1 1 48%}.task-progress{width:25%}.hero-card{min-width:0}.hero-sub{line-height:1.4}}
</style>
