<script setup lang="ts">
import {useVisiblePolling} from './pageActivity'
import {ref,onMounted,onUnmounted,computed} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import TransferDirectories from './TransferDirectories.vue'
import DirectoryPicker from './DirectoryPicker.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()

const tab=ref<'share'|'offline'>('share')
const transfer=ref<any>({target_cid:'',target_label:'',auto_organize:false,bot_auto_organize:false,bot_enabled:false,allowed_user_id:''})
const transferBase=ref(''),transferText=ref(''),jobs=ref<any[]>([]),directoryEditor=ref<any>(null)
const settingsOpen=ref(false)
const offline=ref<any>({target_cid:'',target_label:'',magnet_cid:'',magnet_label:'',ed2k_cid:'',ed2k_label:'',magnet_routes:[],ed2k_routes:[],auto_organize:false,bot_auto_organize:false,magnet_auto_organize:false,ed2k_auto_organize:false,bot_magnet_auto_organize:false,bot_ed2k_auto_organize:false})
const offlineBase=ref(''),offlineText=ref(''),pickerKind=ref(''),routePick=ref<{kind:string;index:number}|null>(null)
const tasks=ref<any[]>([]),running=ref<any>(null),total=ref(0),submissions=ref<any[]>([])
const ready=ref(false),busy=ref(false),error=ref('')
const dirty=computed(()=>ready.value&&(JSON.stringify(transfer.value)!==transferBase.value||JSON.stringify(offline.value)!==offlineBase.value))
const states:Record<string,string>={queued:'排队中',running:'正在转存',completed:'转存完成',paused:'已暂停',cancelled:'已取消',needs_review:'转存结果待核查',waiting_login:'需要登录 115'}

// 转存与离线合并成一条时间线：最新的排最上面，不分类型先后。
const recentRows=computed(()=>{
  const rows:any[]=[]
  for(const j of jobs.value.slice(0,10)){
    rows.push({key:'j'+j.id,kind:'transfer',id:j.id,at:j.updated||0,
      title:j.config?.target_label||'115 转存',
      meta:`${fmtSub(j.updated)} · ${j.config?.origin==='telegram'?'Telegram 机器人':'Web'} · ${j.message||j.stage||''}`,
      state:states[j.state]||j.state,
      tagType:j.state==='completed'?'success':j.state==='running'?'primary':j.state==='needs_review'?'warning':'info'})
  }
  for(const s of submissions.value.slice(0,10)){
    rows.push({key:'s'+s.id,kind:'offline',id:s.id,at:s.submitted_at||0,
      title:s.target_label||'离线下载',
      meta:`${fmtSub(s.submitted_at)} · ${s.message||''}`,
      state:subState(s.state),
      tagType:s.state==='completed'?'success':s.state==='needs_review'?'warning':'info'})
  }
  return rows.sort((a,b)=>b.at-a.at)
})

function ensureRoutes(kind:string){if(!Array.isArray(offline.value[kind+'_routes']))offline.value[kind+'_routes']=[];return offline.value[kind+'_routes'] as any[]}
function addRoute(kind:string){ensureRoutes(kind).push({keywords:'',target_cid:'',target_label:''})}
function dropRoute(kind:string,index:number|string){ensureRoutes(kind).splice(Number(index),1)}
function pickRoute(kind:string,index:number|string){routePick.value={kind,index:Number(index)};pickerKind.value=kind}
function onPicked(folder:any){
  const rp=routePick.value
  if(rp){const r=ensureRoutes(rp.kind)[rp.index];if(r){r.target_cid=folder.id;r.target_label=folder.name}}
  else if(pickerKind.value==='magnet'){offline.value.magnet_cid=folder.id;offline.value.magnet_label=folder.name}
  else if(pickerKind.value==='ed2k'){offline.value.ed2k_cid=folder.id;offline.value.ed2k_label=folder.name}
  else if(pickerKind.value==='transfer'){transfer.value.target_cid=folder.id;transfer.value.target_label=folder.name}
  else{offline.value.target_cid=folder.id;offline.value.target_label=folder.name}
  routePick.value=null;pickerKind.value=''
}
function absorbOffline(data:any){
  offline.value=data
  offline.value.magnet_routes=Array.isArray(offline.value.magnet_routes)?offline.value.magnet_routes:[]
  offline.value.ed2k_routes=Array.isArray(offline.value.ed2k_routes)?offline.value.ed2k_routes:[]
}

async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function refresh(){
  jobs.value=(await props.api('/transfer/jobs')).items
  const r=await props.api('/offline/tasks');tasks.value=r.items||[];total.value=r.total||0;running.value=r.running
  try{const s=await props.api('/offline/submissions?limit=20');submissions.value=s.items||[]}catch{}
}
async function destinationsSaved(){if(dirty.value){ElMessage.warning('下方设置有未保存改动，请先保存或刷新后再修改目录');return}const r=await props.api('/transfer/config');transfer.value=r.config;transferBase.value=JSON.stringify(transfer.value)}
async function save(){
  await run(async()=>{
    const t=await props.api('/transfer/config','PUT',transfer.value);transfer.value=t.config;transferBase.value=JSON.stringify(transfer.value)
    const o=await props.api('/offline/config','PUT',offline.value);absorbOffline(o.config);offlineBase.value=JSON.stringify(offline.value)
    ElMessage.success('入库设置已保存')
  })
}
const intakeText=ref('')
async function submitAll(){await run(async()=>{const r=await props.api('/intake','POST',{text:intakeText.value});intakeText.value='';const parts=[];if(r.share&&r.share.length)parts.push('分享转存 '+r.share.length+' 个');if(r.offline&&r.offline.length)parts.push('离线 '+r.offline.length+' 批');if(r.errors&&r.errors.length)parts.push(r.errors.join('；'));ElMessage.success(parts.join(' · ')||'已提交');await refresh()})}
async function startTransfer(){await run(async()=>{const r=await props.api('/transfer/jobs','POST',{text:transferText.value});transferText.value='';ElMessage.success(`${r.items.length} 个转存任务已排队`);await refresh()})}
async function submitOffline(){await run(async()=>{const r=await props.api('/offline/jobs','POST',{text:offlineText.value});offlineText.value='';ElMessage.success(r.message||`已提交 ${r.count} 个离线任务`);await refresh()})}
async function canLeave(){
  if(directoryEditor.value&&!await directoryEditor.value.canLeave())return false
  if(!dirty.value&&!transferText.value.trim()&&!offlineText.value.trim())return true
  try{await ElMessageBox.confirm('有设置或链接还没保存/提交，离开会丢失。','离开资源入库？',{confirmButtonText:'离开',cancelButtonText:'继续编辑'});return true}catch{return false}
}
function subState(s:string){const map:Record<string,string>={submitting:'提交中',submitted:'等待下载',completed:'已完成',needs_review:'需核查',expired:'已放弃'};return map[s]||s}
function fmtSub(ts:number){if(!ts)return '';return new Date(ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}
const runningLabel=computed(()=>{
  const r:any=running.value
  if(!r)return '当前无运行中任务'
  if(typeof r==='number')return `云端运行中 ${r}`
  const n=r.count??r.errcode??r.task_count??null
  return n===null?'已连接云端':`云端运行中 ${n}`
})
defineExpose({canLeave})
onMounted(()=>{run(async()=>{
  const t=await props.api('/transfer/config');transfer.value=t.config;transferBase.value=JSON.stringify(transfer.value)
  const o=await props.api('/offline/config');absorbOffline(o.config);offlineBase.value=JSON.stringify(offline.value)
  ready.value=true;await refresh()
})})
onUnmounted(()=>{})
useVisiblePolling(async()=>{if(!busy.value)await refresh()},5000)
</script>
<template>
<div class="page-heading"><div><h1>资源入库</h1><p class="muted">分享链接走转存，磁力 / ED2K / 直链走离线下载；各自选择目录，并决定落盘后是否立即整理。</p></div></div>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>

<section class="panel intake-entry"><el-input v-model="intakeText" type="textarea" :rows="7" :disabled="busy" placeholder="粘贴任意链接：115 分享 / magnet:?xt=... / ed2k://|file|... / https://...
可以混着贴，系统会按类型分别入库。" aria-label="入库链接"/><p class="hint">分享 → 转存目录；磁力 / ED2K / 直链 → 各自的离线目录。落盘后是否立即整理由下面的「落盘后处理」决定。</p><div class="button-row"><el-button type="primary" :disabled="busy||!ready||!intakeText.trim()" @click="submitAll">提交</el-button></div></section>


<TransferDirectories ref="directoryEditor" :api="api" :disabled="dirty||busy" :hide-channel="true" @saved="destinationsSaved" @navigate="page=>emit('navigate',page)"/>
<section class="panel intake-options"><div class="settings-heading"><div><h2>落盘后处理 <span v-if="dirty" class="unsaved">未保存</span></h2><p>Web 与 Telegram 机器人分开设置；默认都只落盘</p></div></div>
<div class="settings-content">
<p class="hint">开关关闭：落盘成功即结束，要不要整理由「影视整理」的手动/定时扫描决定。开关打开：落盘完成立即入队本批整理，但目标目录必须在待整理范围内。</p>
<div class="post-table">
 <div class="post-row post-head"><span>入口</span><span>115 分享</span><span>磁力 / 直链</span><span>ED2K</span></div>
 <div class="post-row"><strong>Web</strong><el-switch v-model="transfer.auto_organize" :disabled="busy" aria-label="Web 分享转存后立即整理"/><el-switch v-model="offline.magnet_auto_organize" :disabled="busy" aria-label="Web 磁力整理"/><el-switch v-model="offline.ed2k_auto_organize" :disabled="busy" aria-label="Web ED2K 整理"/></div>
 <div class="post-row"><strong>机器人</strong><el-switch v-model="transfer.bot_auto_organize" :disabled="busy" aria-label="机器人分享转存后立即整理"/><el-switch v-model="offline.bot_magnet_auto_organize" :disabled="busy" aria-label="机器人磁力整理"/><el-switch v-model="offline.bot_ed2k_auto_organize" :disabled="busy" aria-label="机器人 ED2K 整理"/></div>
</div>
<p class="hint">影视探索里点「转存」时落到这个目录；留空就跟随「保存目录」里 Web 的 115 分享目录。机器人接收链接（启用与允许的用户 ID）在「系统设置 → Telegram 通知」里设置。</p>

<div class="button-row"><el-button type="primary" :disabled="busy||!ready||!dirty" @click="save">保存设置</el-button><el-button :disabled="busy" @click="emit('navigate','system')">配置 Telegram 通知</el-button></div>
</div></section>

<section class="panel intake-history"><div class="history-heading"><div><h2>最近记录</h2><p>转存任务与离线提交</p></div><el-button text type="primary" @click="emit('navigate','tasks')">查看全部</el-button></div>
<p v-if="!recentRows.length" class="history-empty">还没有记录</p>
<div v-for="row in recentRows" :key="row.key" class="history-row"><span class="history-id">#{{row.id}}</span><div class="history-main"><strong :title="row.title">{{row.title}}</strong><span>{{row.meta}}</span></div><span class="history-kind" :class="'is-'+row.kind">{{row.kind==='offline'?'离线':'转存'}}</span><el-tag size="small" :type="row.tagType" effect="light">{{row.state}}</el-tag></div>
<div v-if="submissions.length" class="button-row"><el-tag size="small" type="info">{{runningLabel}}</el-tag></div>
</section>

<DirectoryPicker v-if="pickerKind" :api="api" @select="onPicked" @close="pickerKind='';routePick=null"/>
</template>

<style scoped>
.intake-entry :deep(.el-textarea__inner){min-height:130px;padding:14px 16px;background:var(--surface-soft);box-shadow:0 0 0 1px var(--line) inset}
.intake-tabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.intake-tabs button{border:1px solid var(--line);background:transparent;color:var(--muted);padding:8px 14px;border-radius:8px;font-size:13px;cursor:pointer}
.intake-tabs button.active{color:var(--accent);border-color:var(--accent);font-weight:600}
.settings-heading,.history-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}
.settings-heading h2,.history-heading h2{margin:0;font-size:16px}
.settings-heading p,.history-heading p{font-size:12px;color:var(--muted);margin:5px 0 0}
.settings-content{margin-top:18px;padding-top:4px;border-top:1px solid var(--line)}
.unsaved{font-size:11px;color:#b77b2b;margin-left:6px;font-weight:400}
.intake-options h3{margin-top:24px}
.history-empty{color:var(--muted);font-size:13px;margin:20px 0 2px}
.history-row{display:flex;align-items:center;gap:12px;padding:13px 0;border-top:1px solid var(--line)}
.history-row:first-of-type{margin-top:14px}
.history-kind{flex:none;width:30px;text-align:center;font-size:11px;line-height:18px;border-radius:4px;background:var(--line);color:var(--muted)}
.history-kind.is-offline{background:rgba(64,158,255,.14);color:#409eff}
.history-kind.is-transfer{background:rgba(103,194,58,.14);color:#67c23a}
.history-id{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;flex:none;min-width:52px;text-align:right}
.history-main{display:grid;gap:4px;flex:1;min-width:0}
.history-main strong,.history-main span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.history-main strong{font-size:13px}
.history-main span{font-size:12px;color:var(--muted)}
.post-table{display:grid;gap:6px;margin:14px 0}
.post-row{display:grid;grid-template-columns:120px 1fr 1fr 1fr;align-items:center;gap:12px;padding:10px 12px;border-radius:8px;background:var(--surface-soft)}
.post-row.post-head{background:transparent;padding:0 12px;font-size:12px;color:var(--muted)}
.post-row strong{font-size:13px}
@media(max-width:760px){.history-row{align-items:flex-start;flex-wrap:wrap}.history-main{flex-basis:65%}.history-main strong,.history-main span{white-space:normal;overflow-wrap:anywhere;line-height:1.5}}
</style>
