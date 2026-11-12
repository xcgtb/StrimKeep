<script setup lang="ts">
import {ref,shallowRef,computed,watch,nextTick,onUnmounted} from 'vue'
import Tasks from './Tasks.vue'
import {usePageActivity} from './pageActivity'
import {levelOf} from './logLevel'
import {ElMessage} from 'element-plus'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()

const tab=ref('live'),rows=shallowRef<any[]>([]),live=ref(false),paused=ref(false)
const level=ref('all'),kind=ref(''),search=ref(''),term=ref(''),selected=shallowRef<any>(null)
const scrollTop=ref(0),viewport=ref<HTMLElement>(),loading=ref(false),older=ref(true),showProgress=ref(true)
let stream:EventSource|undefined,visible=false,frame=0,pending:any[]=[],queryTimer:ReturnType<typeof setTimeout>|undefined
// 手机上正文一行装不下，改成两行；行高跟着变，虚拟滚动才不串位。
const media=window.matchMedia('(max-width:760px)')
const narrow=ref(media.matches)
const onMedia=()=>{narrow.value=media.matches}
media.addEventListener('change',onMedia)
onUnmounted(()=>media.removeEventListener('change',onMedia))
const height=computed(()=>narrow.value?92:58)

const KINDS:Record<string,string>={progress:'进度',organize:'整理',queue:'队列',schedule:'调度',emby:'Emby','115':'115',cloud:'风控',system:'系统',error:'异常',channel:'频道',share:'分享',share_organize:'分享整理',play:'播放',output:'输出',bot:'机器人',transfer:'转存',settings:'设置',token:'令牌'}
const kindLabel=(value:string)=>KINDS[value]||value
const kindOptions=computed(()=>{const seen=new Set(rows.value.map(r=>r.kind));return Object.keys(KINDS).filter(k=>seen.has(k))})

function jobOf(message:string){const m=/^任务\s*(\d+)\s*[：:]\s*/.exec(String(message||''));return m?m[1]:''}
function bodyOf(message:string){return String(message||'').replace(/^任务\s*\d+\s*[：:]\s*/,'')}
function clock(at:number){
  const date=new Date(at*1000),now=new Date()
  const time=date.toLocaleTimeString('zh-CN',{hour12:false})
  return date.toDateString()===now.toDateString()?time:date.toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
}
function fullTime(at:number){return new Date(at*1000).toLocaleString('zh-CN',{hour12:false})}

const filtered=computed(()=>rows.value.filter(row=>{
  if(!showProgress.value&&row.kind==='progress')return false
  if(kind.value&&row.kind!==kind.value)return false
  if(level.value!=='all'&&levelOf(row)!==level.value)return false
  if(term.value&&!String(row.message||'').toLowerCase().includes(term.value))return false
  return true
}))
const start=computed(()=>Math.max(0,Math.floor(scrollTop.value/height.value)-4))
const shown=computed(()=>filtered.value.slice(start.value,start.value+24))
const counts=computed(()=>{let warn=0,error=0;for(const row of rows.value){const l=levelOf(row);if(l==='error')error++;else if(l==='warn')warn++}return {warn,error}})

function merge(items:any[]){const ids=new Set(items.map(r=>r.id));rows.value=[...items,...rows.value.filter(r=>!ids.has(r.id))].sort((a,b)=>b.id-a.id).slice(0,5000)}
function flush(){frame=0;if(!paused.value&&pending.length){merge(pending);pending=[]}}
function disconnect(){stream?.close();stream=undefined;live.value=false;if(frame)cancelAnimationFrame(frame);frame=0;pending=[]}
function connect(){disconnect();if(!visible||tab.value!=='live')return;stream=new EventSource('/api/v1/logs/events?after='+(rows.value[0]?.id||0));stream.onopen=()=>live.value=true;stream.addEventListener('logs',e=>{if(paused.value)return;try{pending.push(...JSON.parse((e as MessageEvent).data).items);if(!frame)frame=requestAnimationFrame(flush)}catch{live.value=false}});stream.onerror=()=>live.value=false}
async function load(before=false){if(loading.value)return;loading.value=true;try{const suffix=before?'&before='+rows.value.at(-1)?.id:'';const items=(await props.api('/logs?limit=200'+suffix)).items;merge(items);if(before)older.value=items.length===200;if(!before)connect()}catch(e){console.warn(e)}finally{loading.value=false}}
async function download(){
  const params=new URLSearchParams({limit:'5000'})
  if(kind.value)params.set('kind',kind.value)
  if(term.value)params.set('q',term.value)
  try{
    const res=await fetch('/api/v1/logs/export?'+params.toString(),{credentials:'include'})
    if(!res.ok)throw new Error('HTTP '+res.status)
    const blob=await res.blob(),url=URL.createObjectURL(blob),link=document.createElement('a')
    link.href=url;link.download='strimkeep-logs-'+new Date().toISOString().slice(0,19).replace(/[:T]/g,'')+'.txt'
    link.click();URL.revokeObjectURL(url)
    ElMessage.success('已导出当前筛选下的日志')
  }catch(err){ElMessage.error('导出失败：'+(err as Error).message)}
}
watch(search,value=>{if(queryTimer)clearTimeout(queryTimer);queryTimer=setTimeout(()=>term.value=value.toLowerCase(),180)})
watch([term,kind,level,showProgress],async()=>{scrollTop.value=0;await nextTick();if(viewport.value)viewport.value.scrollTop=0})
watch(paused,value=>{if(!value)void load()})
watch(tab,value=>{if(value==='live'&&visible)void load();else disconnect()})
usePageActivity(()=>{visible=true;if(tab.value==='live')void load()},()=>{visible=false;disconnect();if(queryTimer)clearTimeout(queryTimer)})
</script>
<template>
<div class="page-title"><div><h1>实时日志</h1><p class="muted">谁在做什么、做到哪一步、有没有出问题。</p></div><el-tag :type="live?'success':'info'">{{live?'实时连接':'连接待恢复'}}</el-tag></div>
<el-tabs v-model="tab">
 <el-tab-pane label="实时日志" name="live"/>
 <el-tab-pane label="任务队列与记录" name="tasks"/>
</el-tabs>
<Tasks v-if="tab==='tasks'" :api="api" embedded/>
<section v-else class="panel">
 <div class="log-toolbar">
  <el-radio-group v-model="level" size="small">
   <el-radio-button value="all">全部</el-radio-button>
   <el-radio-button value="warn">注意 {{counts.warn}}</el-radio-button>
   <el-radio-button value="error">异常 {{counts.error}}</el-radio-button>
  </el-radio-group>
  <el-select v-model="kind" clearable placeholder="全部类别" style="width:130px">
   <el-option v-for="k in kindOptions" :key="k" :value="k" :label="kindLabel(k)"/>
  </el-select>
  <el-input v-model="search" clearable placeholder="搜索内容，例如频道名、文件名" aria-label="搜索日志" style="max-width:280px"/>
  
  <el-button @click="paused=!paused">{{paused?'继续显示':'暂停'}}</el-button>
  <el-button :loading="loading" @click="load(false)">刷新</el-button>
  <el-button :disabled="!rows.length" @click="download">下载日志</el-button>
 </div>

 <div ref="viewport" class="live-log virtual-log" role="log" @scroll="scrollTop=($event.target as HTMLElement).scrollTop">
  <div :style="{height:start*height+'px'}"/>
  <button v-for="r in shown" :key="r.id" class="log-row" :class="['lv-'+levelOf(r),narrow?'narrow':'']" :style="{height:height+'px'}" @click="selected=r">
   <time class="log-time">{{clock(r.at)}}</time>
   <span class="log-dot" aria-hidden="true"/>
   <span class="log-kind">{{kindLabel(r.kind)}}</span>
   <span class="log-job" v-if="jobOf(r.message)">#{{jobOf(r.message)}}</span>
   <span class="log-body">{{bodyOf(r.message)}}</span>
  </button>
  <div :style="{height:Math.max(0,filtered.length-start-shown.length)*height+'px'}"/>
  <el-empty v-if="!filtered.length" description="这个筛选下还没有日志"/>
 </div>

 <div class="button-row">
  <span class="hint">已载入 {{rows.length}} 条，含每一步的执行细节</span>
  <el-button v-if="older&&rows.length<5000" :loading="loading" @click="load(true)">载入更早</el-button>
 </div>
</section>

<el-drawer :model-value="!!selected" :title="selected?kindLabel(selected.kind):'日志'" size="min(560px,100%)" @close="selected=null">
 <template v-if="selected">
  <p class="muted">{{fullTime(selected.at)}}</p>
  <p v-if="jobOf(selected.message)">任务 <b>#{{jobOf(selected.message)}}</b></p>
  <pre class="log-detail">{{bodyOf(selected.message)}}</pre>
 </template>
</el-drawer>
</template>
<style scoped>
.log-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
.log-row{display:flex;align-items:center;gap:10px;width:100%;border:0;background:transparent;text-align:left;padding:0 8px;border-radius:8px;cursor:pointer;border-bottom:1px solid var(--line)}
.log-row:hover{background:var(--surface-soft)}
.log-time{flex:0 0 92px;color:var(--muted);font-variant-numeric:tabular-nums;font-size:12px}
.log-dot{flex:0 0 6px;width:6px;height:6px;border-radius:50%;background:var(--muted)}
.lv-warn .log-dot{background:#d9a441}
.lv-error .log-dot{background:#d05a5a}
.log-kind{flex:0 0 58px;color:var(--muted);font-size:12px}
.log-job{flex:0 0 auto;color:var(--accent);font-size:12px;font-variant-numeric:tabular-nums}
.log-body{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;color:var(--text)}
.lv-error .log-body{color:#c0504d}
.log-detail{margin:12px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.8;background:var(--surface-soft);padding:14px;border-radius:10px;color:var(--text)}
@media (max-width:760px){
 .log-row{flex-wrap:wrap;align-items:baseline;gap:2px 8px;padding:8px 6px}
 .log-time{flex:0 0 auto;font-size:11px}
 .log-dot{display:none}
 .log-kind{flex:0 0 auto;font-size:11px}
 .log-job{font-size:11px}
 .log-body{flex:0 0 100%;white-space:normal;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.55;font-size:13px}
 .log-toolbar{gap:8px}
 .log-toolbar .el-radio-group{flex:0 0 100%}
 .log-toolbar .el-input{flex:1 1 100%;max-width:none!important}
}
</style>

<style scoped>

.log-toolbar{padding:13px;border:1px solid var(--line);border-radius:9px;background:var(--surface-soft)}.log-toolbar :deep(.el-input__wrapper),.log-toolbar :deep(.el-select__wrapper){background:var(--surface)}.log-row{border-radius:0}.log-kind{font-weight:600}.log-job{background:var(--el-color-primary-light-9);border-radius:5px;padding:2px 5px}.live-log{scrollbar-width:thin}.log-detail{border:1px solid var(--line)}
@media(max-width:760px){.log-toolbar{padding:10px}.log-toolbar :deep(.el-radio-group){justify-content:flex-start}.log-toolbar :deep(.el-switch){margin-right:auto}.log-row{padding-inline:8px}}
</style>
