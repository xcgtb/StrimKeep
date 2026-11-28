<script setup lang="ts">
import PageHeader from './PageHeader.vue'
import {computed,onMounted,ref} from 'vue'
import {useVisiblePolling} from './pageActivity'
import {ElMessage,ElMessageBox} from 'element-plus'
import {Refresh} from '@element-plus/icons-vue'

const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
type Row={media_type:'movie'|'tv';tmdb_id:number;season:number;title:string;state:string;message:string;
  enabled:number|boolean;share_action:'share'|'transfer';backfill:number|boolean;
  auto_switch:number|boolean;minimum_resolution:number;preferred_sources:string[];
  last_check:number;next_check:number;
  details:{present?:number[];missing?:number[];submitted?:number[];needs_review?:number[];candidates?:unknown[];sources?:{name:string;error?:string}[]};
  attempts:{episode:number;job_id?:number;inbox_id?:number}[]}
const rows=ref<Row[]>([]),busy=ref(false),error=ref(''),filter=ref<'all'|'active'|'review'|'completed'>('all')
const labels:Record<string,string>={pending:'等待检查',waiting:'待开播',caught_up:'已追齐',searching:'等待资源',review:'待核查',submitted:'等待入库',error:'检查失败',completed:'已入库'}
const endpoint=(row:Row)=>row.media_type==='movie'?`/explore/subscriptions/movie/${row.tmdb_id}`:`/explore/subscriptions/${row.tmdb_id}/${row.season}`
const season=(row:Row)=>row.media_type==='movie'?'电影':`S${String(row.season).padStart(2,'0')}`
const label=(row:Row)=>!row.enabled?'已暂停':labels[row.state]||row.state
const visible=computed(()=>rows.value.filter(row=>filter.value==='all'
  || filter.value==='active'&&row.enabled&&row.state!=='completed'
  || filter.value==='review'&&['review','error'].includes(row.state)
  || filter.value==='completed'&&row.state==='completed'))
const counts=computed(()=>({all:rows.value.length,active:rows.value.filter(r=>r.enabled&&r.state!=='completed').length,
  review:rows.value.filter(r=>['review','error'].includes(r.state)).length,
  completed:rows.value.filter(r=>r.state==='completed').length}))
const countFor=(id:string)=>Number((counts.value as Record<string,number>)[id]||0)
function setFilter(id:string){filter.value=id as typeof filter.value}
const fmt=(ts:number)=>ts?new Date(ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'
const episodes=(values?:number[])=>values?.length?values.slice(0,12).map(n=>'E'+String(n).padStart(2,'0')).join('、')+(values.length>12?` 等 ${values.length} 集`:''):''
async function fetchRows(){rows.value=(await props.api('/explore/subscriptions')).items||[]}
async function load(){if(busy.value)return;busy.value=true;error.value='';try{await fetchRows()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function retry(row:Row){void run(async()=>{await props.api(endpoint(row)+'/retry','POST');ElMessage.success('已安排重新检查');await fetchRows()})}
function toggle(row:Row){void run(async()=>{await props.api(endpoint(row),'PUT',{share_action:row.share_action,backfill:!!row.backfill,
  auto_switch:!!row.auto_switch,minimum_resolution:row.minimum_resolution,preferred_sources:row.preferred_sources||[],enabled:!row.enabled});await fetchRows()})}
async function remove(row:Row){
  try{await ElMessageBox.confirm(`取消《${row.title}》${season(row)}的订阅？`,'取消订阅',{type:'warning',confirmButtonText:'取消订阅',cancelButtonText:'保留'})}catch{return}
  void run(async()=>{await props.api(endpoint(row),'DELETE');await fetchRows()})
}
onMounted(load)
useVisiblePolling(load,15000)
</script>
<template>
<PageHeader title="追更订阅" description="管理电影与剧集的自动检查、资源提交和入库状态。">
  <template #actions><div class="button-row"><el-button :icon="Refresh" :loading="busy" @click="load">刷新</el-button><el-button type="primary" @click="emit('navigate','explore')">去影视探索添加</el-button></div></template>
</PageHeader>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>
<div class="subscription-tabs" role="tablist">
  <button v-for="item in [{id:'all',label:'全部'},{id:'active',label:'追更中'},{id:'review',label:'待核查'},{id:'completed',label:'已入库'}]"
    :key="item.id" :class="{active:filter===item.id}" @click="setFilter(item.id)">{{item.label}} {{countFor(item.id)}}</button>
</div>
<el-empty v-if="!busy&&!visible.length" description="还没有订阅，去影视探索选择电影或剧集"/>
<div v-else class="subscription-grid" :aria-busy="busy">
  <article v-for="row in visible" :key="`${row.media_type}:${row.tmdb_id}:${row.season}`" class="subscription-card">
    <header><div><h3>{{row.title}}</h3><p>{{season(row)}} · {{row.share_action==='share'?'分享 STRM':'转存到 115'}}</p></div>
      <el-tag :type="row.state==='completed'?'success':['review','error'].includes(row.state)?'warning':'info'">{{label(row)}}</el-tag></header>
    <p v-if="row.message" class="hint">{{row.message}}</p>
    <p class="hint">上次检查 {{fmt(row.last_check)}}<span v-if="row.enabled&&row.next_check"> · 下次 {{fmt(row.next_check)}}</span></p>
    <p v-if="row.details.present?.length" class="hint">已入库 {{episodes(row.details.present)}}</p>
    <p v-if="row.details.missing?.length" class="hint">待补 {{episodes(row.details.missing)}}</p>
    <p v-if="row.details.submitted?.length" class="hint">已提交 {{episodes(row.details.submitted)}}</p>
    <p v-if="row.details.needs_review?.length" class="hint">待核查 {{episodes(row.details.needs_review)}}</p>
    <p v-if="row.details.sources?.some(s=>s.error)" class="hint">搜索失败 {{row.details.sources.filter(s=>s.error).map(s=>s.name).join('、')}}</p>
    <div class="subscription-actions">
      <el-button v-if="['review','error'].includes(row.state)" size="small" type="warning" plain @click="retry(row)">重试</el-button>
      <el-button size="small" :disabled="row.state==='completed'" @click="toggle(row)">{{row.enabled?'暂停':'恢复'}}</el-button>
      <el-button size="small" @click="emit('navigate','explore')">编辑</el-button>
      <el-button size="small" text type="danger" @click="remove(row)">取消订阅</el-button>
    </div>
  </article>
</div>
</template>

<style scoped>
.subscription-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.subscription-tabs button{border:1px solid var(--line);background:var(--surface);color:var(--muted);border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit;font-size:13px}
.subscription-tabs button.active{border-color:var(--accent);color:var(--accent);font-weight:600}
.subscription-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:14px}
.subscription-card{border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:16px;min-width:0}
.subscription-card header{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
.subscription-card h3{margin:0;font-size:16px;overflow-wrap:anywhere}
.subscription-card header p{margin:5px 0 0;color:var(--muted);font-size:12px}
.subscription-card .hint{margin:7px 0;line-height:1.5;overflow-wrap:anywhere}
.subscription-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
.subscription-actions :deep(.el-button){margin-left:0}
</style>
