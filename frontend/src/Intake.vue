<script setup lang="ts">
import {useVisiblePolling} from './pageActivity'
import {ref,onMounted,onUnmounted,computed} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import {useRoute} from 'vue-router'
import TransferDirectories from './TransferDirectories.vue'
import DirectoryPicker from './DirectoryPicker.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const route=useRoute()

const tab=ref<'share'|'offline'>('share')
const transfer=ref<any>({target_cid:'',target_label:'',auto_organize:false,bot_auto_organize:false,bot_enabled:false,allowed_user_id:''})
const transferBase=ref(''),jobs=ref<any[]>([]),directoryEditor=ref<any>(null)
const settingsOpen=ref(false)
const offline=ref<any>({target_cid:'',target_label:'',magnet_cid:'',magnet_label:'',ed2k_cid:'',ed2k_label:'',magnet_routes:[],ed2k_routes:[],auto_organize:false,bot_auto_organize:false,magnet_auto_organize:false,ed2k_auto_organize:false,bot_magnet_auto_organize:false,bot_ed2k_auto_organize:false})
const offlineBase=ref(''),pickerKind=ref(''),routePick=ref<{kind:string;index:number}|null>(null)
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
async function destinationsSaved(){if(dirty.value){ElMessage.warning('下方设置有未保存改动，请先保存或刷新后再修改目录');return}ready.value=false;try{const [t,o]=await Promise.all([props.api('/transfer/config'),props.api('/offline/config')]);transfer.value=t.config;transferBase.value=JSON.stringify(transfer.value);absorbOffline(o.config);offlineBase.value=JSON.stringify(offline.value);ready.value=true}catch(e){error.value='目录已保存，但设置刷新失败；请刷新页面后再修改：'+(e as Error).message}}
async function save(){
  await run(async()=>{
    const t=await props.api('/transfer/config','PUT',transfer.value);transfer.value=t.config;transferBase.value=JSON.stringify(transfer.value)
    const o=await props.api('/offline/config','PUT',offline.value);absorbOffline(o.config);offlineBase.value=JSON.stringify(offline.value)
    ElMessage.success('入库设置已保存')
  })
}
const intakeText=ref('')
async function submitAll(){await run(async()=>{
  let r:any
  try{r=await props.api('/intake','POST',{text:intakeText.value})}catch(e){throw new Error('提交结果未确认，请先到任务中心核对，避免重复提交。'+(e as Error).message)}
  const parts=[];if(r.share?.length)parts.push('分享转存 '+r.share.length+' 个');if(r.offline?.length)parts.push('离线 '+r.offline.length+' 批')
  if(r.errors?.length){error.value=(parts.length?parts.join('、')+'已提交。请勿整批重复提交；':'')+r.errors.join('；')+'。输入已保留，请核对后只提交未成功的链接。';ElMessage.warning('部分链接未提交，请查看原因')}
  else if(!parts.length){error.value='没有识别到可提交的链接，请检查内容。';ElMessage.warning(error.value)}
  else {intakeText.value='';ElMessage.success(parts.join(' · ')+'已提交')}
  try{await refresh()}catch(e){error.value+=(error.value?'；':'')+'提交结果已返回，但最近记录刷新失败，请到任务中心核对：'+(e as Error).message}
})}
async function canLeave(){
  if(directoryEditor.value&&!await directoryEditor.value.canLeave())return false
  if(!dirty.value&&!intakeText.value.trim())return true
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
function beforeUnload(e:BeforeUnloadEvent){if(route.name==='transfer'&&(dirty.value||intakeText.value.trim())){e.preventDefault();e.returnValue=''}}
onMounted(()=>{run(async()=>{
  const t=await props.api('/transfer/config');transfer.value=t.config;transferBase.value=JSON.stringify(transfer.value)
  const o=await props.api('/offline/config');absorbOffline(o.config);offlineBase.value=JSON.stringify(offline.value)
  ready.value=true;await refresh()
});window.addEventListener('beforeunload',beforeUnload)})
onUnmounted(()=>window.removeEventListener('beforeunload',beforeUnload))
useVisiblePolling(async()=>{if(!busy.value)await refresh()},5000)
</script>
<template>
<div class="page-heading"><div><h1>资源入库</h1><p class="muted">分享链接走转存，磁力 / ED2K / 直链走离线下载；各自选择目录，并决定落盘后是否立即整理。</p></div></div>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>

<section class="panel intake-entry"><el-input v-model="intakeText" type="textarea" :rows="7" :disabled="busy" placeholder="粘贴任意链接：115 分享 / magnet:?xt=... / ed2k://|file|... / https://...
可以混着贴，系统会按类型分别入库。" aria-label="入库链接"/><p class="hint">分享 → 转存目录；磁力 / ED2K / 直链 → 各自的离线目录。落盘后是否立即整理由下面的「落盘后处理」决定。</p><div class="button-row"><el-button type="primary" :disabled="busy||!ready||!intakeText.trim()" @click="submitAll">提交</el-button></div></section>


<TransferDirectories ref="directoryEditor" :api="api" :disabled="dirty||busy||!ready" :hide-channel="true" @saved="destinationsSaved" @navigate="page=>emit('navigate',page)"/>
<details class="panel intake-options offline-advanced"><summary><strong>落盘后处理 <span v-if="dirty" class="unsaved">未保存</span></strong><span>Web 与机器人分开设置</span></summary>
<div class="settings-content">
<p class="hint">开关关闭：落盘成功即结束，要不要整理由「影视整理」的手动/定时扫描决定。开关打开：落盘完成立即入队本批整理，但目标目录必须在待整理范围内。</p>
<div class="post-table">
 <div class="post-row post-head"><span>入口</span><span>115 分享</span><span>磁力 / 直链</span><span>ED2K</span></div>
 <div class="post-row"><strong>Web</strong><el-switch v-model="transfer.auto_organize" :disabled="busy" aria-label="Web 分享转存后立即整理"/><el-switch v-model="offline.magnet_auto_organize" :disabled="busy" aria-label="Web 磁力整理"/><el-switch v-model="offline.ed2k_auto_organize" :disabled="busy" aria-label="Web ED2K 整理"/></div>
 <div class="post-row"><strong>机器人</strong><el-switch v-model="transfer.bot_auto_organize" :disabled="busy" aria-label="机器人分享转存后立即整理"/><el-switch v-model="offline.bot_magnet_auto_organize" :disabled="busy" aria-label="机器人磁力整理"/><el-switch v-model="offline.bot_ed2k_auto_organize" :disabled="busy" aria-label="机器人 ED2K 整理"/></div>
</div>
<p class="hint">机器人接收链接（启用与允许的用户 ID）在「系统设置 → Telegram 通知」里设置。</p>

<div class="button-row"><el-button type="primary" :disabled="busy||!ready||!dirty" @click="save">保存设置</el-button><el-button :disabled="busy" @click="emit('navigate','system')">配置 Telegram 通知</el-button></div>
</div></details>

<details class="panel offline-advanced"><summary><strong>离线下载目录与分流</strong><span>直链目录和关键词分流</span></summary>
<p class="hint">HTTP、HTTPS 和 FTP 直链使用单独目录；磁力和 ED2K 的默认目录在上方“保存目录”设置。</p>
<div class="offline-destination"><div><strong>直链保存目录</strong><p>{{offline.target_label||offline.target_cid||'未设置，直链无法提交'}}</p></div><div class="button-row"><el-button :disabled="busy" @click="pickerKind='target'">选择目录</el-button><el-button v-if="offline.target_cid" text :disabled="busy" @click="offline.target_cid='';offline.target_label=''">清除</el-button></div></div>
<div v-for="kind in ['magnet','ed2k']" :key="kind" class="offline-routes">
 <h3>{{kind==='magnet'?'磁力':'ED2K'}}关键词分流</h3>
 <p class="hint">网页提交的文本命中关键词时使用对应目录；未命中使用默认目录。机器人使用自己的默认目录，不应用这里的分流。每次提交的同类链接共用一个匹配结果。</p>
 <div v-for="(item,index) in ensureRoutes(kind)" :key="index" class="offline-route">
  <div class="offline-route-head"><strong>规则 {{Number(index)+1}}</strong><el-button text type="danger" :disabled="busy" @click="dropRoute(kind,index)">删除</el-button></div>
  <el-input v-model="item.keywords" :disabled="busy" placeholder="关键词，英文逗号分隔"/>
  <div class="offline-route-head"><span>{{item.target_label||item.target_cid||'尚未选择目录'}}</span><el-button :disabled="busy" @click="pickRoute(kind,index)">选择目录</el-button></div>
 </div>
 <el-button :disabled="busy||ensureRoutes(kind).length>=20" @click="addRoute(kind)">添加分流规则</el-button>
</div>
<el-button type="primary" :disabled="busy||!ready||!dirty" @click="save">保存入库设置</el-button>
</details>

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
.offline-advanced>summary{display:flex;justify-content:space-between;gap:12px;align-items:center;cursor:pointer}.offline-advanced>summary span{font-size:12px;color:var(--muted)}.offline-destination,.offline-route-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.offline-destination{padding:12px 0;border-top:1px solid var(--line)}.offline-destination p{color:var(--muted);font-size:12px;margin:6px 0}.offline-routes{padding:8px 0 16px;border-top:1px solid var(--line)}.offline-routes h3{font-size:14px}.offline-route{display:grid;gap:8px;padding:12px;margin:8px 0;border:1px solid var(--line);border-radius:8px}.offline-route-head span{min-width:0;overflow-wrap:anywhere;color:var(--muted);font-size:12px}
@media(max-width:760px){.history-row{align-items:flex-start;flex-wrap:wrap}.history-main{flex-basis:65%}.history-main strong,.history-main span{white-space:normal;overflow-wrap:anywhere;line-height:1.5}}
</style>
