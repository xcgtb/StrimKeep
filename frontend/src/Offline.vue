<script setup lang="ts">
import {useVisiblePolling} from './pageActivity'
import {ref,onMounted,onUnmounted,computed} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import DirectoryPicker from './DirectoryPicker.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const config=ref<any>({target_cid:'',target_label:'',magnet_cid:'',magnet_label:'',ed2k_cid:'',ed2k_label:'',magnet_routes:[],ed2k_routes:[],auto_organize:false,bot_auto_organize:false,scan_minutes:5})
const baseline=ref(''),ready=ref(false),busy=ref(false),error=ref(''),pickerKind=ref(''),routePick=ref<{kind:string;index:number}|null>(null),text=ref(''),tasks=ref<any[]>([]),running=ref<any>(null),total=ref(0)
const submissions=ref<any[]>([])
const dirty=computed(()=>ready.value&&JSON.stringify(config.value)!==baseline.value)
const base=computed<any>(()=>{try{return JSON.parse(baseline.value)||{}}catch{return {}}})
const PANEL_FIELDS:Record<string,string[]>={target:['target_cid','target_label','scan_minutes'],magnet:['magnet_cid','magnet_label','magnet_routes'],ed2k:['ed2k_cid','ed2k_label','ed2k_routes']}
function panelState(kind:string){if(!ready.value)return '';const f=PANEL_FIELDS[kind]||[];return JSON.stringify(f.map(k=>config.value[k]))===JSON.stringify(f.map(k=>base.value[k]))?'saved':'dirty'}
async function savePanel(_kind:string){await save()}

function ensureRoutes(kind:string){if(!Array.isArray(config.value[kind+'_routes']))config.value[kind+'_routes']=[];return config.value[kind+'_routes'] as any[]}
function addRoute(kind:string){ensureRoutes(kind).push({keywords:'',target_cid:'',target_label:''})}
function dropRoute(kind:string,index:number|string){ensureRoutes(kind).splice(Number(index),1)}
function pickRoute(kind:string,index:number|string){routePick.value={kind,index:Number(index)};pickerKind.value=kind}
function onPicked(folder:any){
  const rp=routePick.value
  if(rp){const r=ensureRoutes(rp.kind)[rp.index];if(r){r.target_cid=folder.id;r.target_label=folder.name}}
  else if(pickerKind.value==='magnet'){config.value.magnet_cid=folder.id;config.value.magnet_label=folder.name}
  else if(pickerKind.value==='ed2k'){config.value.ed2k_cid=folder.id;config.value.ed2k_label=folder.name}
  else{config.value.target_cid=folder.id;config.value.target_label=folder.name}
  routePick.value=null;pickerKind.value=''
}
function absorbConfig(data:any){
  config.value=data
  config.value.magnet_routes=Array.isArray(config.value.magnet_routes)?config.value.magnet_routes:[]
  config.value.ed2k_routes=Array.isArray(config.value.ed2k_routes)?config.value.ed2k_routes:[]
}

async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function refresh(){const r=await props.api('/offline/tasks');tasks.value=r.items||[];total.value=r.total||0;running.value=r.running;try{const s=await props.api('/offline/submissions?limit=20');submissions.value=s.items||[]}catch{}}
async function save(){await run(async()=>{const r=await props.api('/offline/config','PUT',config.value);absorbConfig(r.config);baseline.value=JSON.stringify(config.value);ElMessage.success('所有离线目录及分流设置已保存')})}
async function submit(){await run(async()=>{const r=await props.api('/offline/jobs','POST',{text:text.value});text.value='';ElMessage.success(r.message||`已提交 ${r.count} 个离线任务`);await refresh()})}
async function canLeave(){if(!dirty.value&&!text.value.trim())return true;try{await ElMessageBox.confirm('当前有未保存设置或未提交链接，刷新网页后会丢失。确定切换？','离开离线下载',{confirmButtonText:'暂存并切换',cancelButtonText:'继续编辑'});return true}catch{return false}}
function percentText(v:any){const n=Number(v)||0;return n<=1?`${Math.round(n*100)}%`:`${Math.round(n)}%`}
function statusText(s:any){const map:Record<string,string>={0:'等待中',1:'下载中',2:'已完成',3:'失败',success:'已完成',failed:'失败',running:'下载中',waiting:'等待中'};return map[String(s)]||String(s||'—')}
function subState(s:string){const map:Record<string,string>={submitting:'提交中',submitted:'等待下载',completed:'已完成',needs_review:'需核查',expired:'已放弃'};return map[s]||s}
async function expireOld(){await run(async()=>{const r=await props.api('/offline/submissions/expire?days=7','POST');ElMessage.success('已放弃 '+r.expired+' 条超过 7 天的待核查');await refresh()})}
function fmtSub(ts:number){if(!ts)return '';return new Date(ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}
const runningLabel=computed(()=>{
  const r:any=running.value
  if(!r)return '当前无运行中任务'
  if(typeof r==='number')return `云端运行中 ${r}`
  const n=r.count??r.errcode??r.task_count??null
  return n===null?'已连接云端':`云端运行中 ${n}`
})
defineExpose({canLeave})
onMounted(()=>{run(async()=>{const r=await props.api('/offline/config');absorbConfig(r.config);baseline.value=JSON.stringify(config.value);ready.value=true;await refresh()})})
onUnmounted(()=>{})
useVisiblePolling(async()=>{if(!busy.value)await refresh()},5000)
</script>
<template>
<div class="page-heading"><div><h1>离线下载</h1><p class="muted">提交磁力、电驴或直链，由 115 云端下载到指定目录。三类链接各自有独立目录与关键词分流。</p></div></div>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>

<section class="panel offline-submit"><h2>提交链接</h2>
<el-form label-position="top"><el-form-item label="链接（一行一个，支持 magnet / ed2k / http / https / ftp）"><el-input v-model="text" type="textarea" :rows="6" :disabled="busy" placeholder="magnet:?xt=urn:btih:...&#10;ed2k://|file|...&#10;https://example.com/file.mkv"/></el-form-item></el-form>
<p class="hint offline-destination-label">{{!ready?'正在读取目录设置':dirty?'目录设置预览（含未保存修改）':'已保存的目录设置'}}</p><div class="offline-destinations" aria-label="目录设置"><div><span>直链目录</span><strong :title="config.target_label||config.target_cid||'未设置'">{{config.target_label||config.target_cid||'未设置'}}</strong></div><div><span>磁力目录</span><strong :title="config.magnet_label||config.magnet_cid||'未设置'">{{config.magnet_label||config.magnet_cid||'未设置'}}</strong></div><div><span>ED2K 目录</span><strong :title="config.ed2k_label||config.ed2k_cid||'未设置'">{{config.ed2k_label||config.ed2k_cid||'未设置'}}</strong></div></div>
  <div class="workflow-switch-row"><div><strong>Web 提交离线后立即整理</strong><p>开启后落盘完成立即入队（目标目录必须在待整理范围内）；关闭时只落盘，由「影视整理」扫描决定。</p></div><el-switch v-model="config.auto_organize" :disabled="busy" aria-label="Web 离线后立即整理"/></div>
  <div class="workflow-switch-row"><div><strong>机器人提交离线后立即整理</strong><p>与 Web 分开设置，互不影响。</p></div><el-switch v-model="config.bot_auto_organize" :disabled="busy" aria-label="机器人离线后立即整理"/></div>
<div class="button-row"><el-button type="primary" :disabled="busy||!ready||!text.trim()" @click="submit">提交离线任务</el-button><span v-if="dirty" class="hint">目录改动尚未保存。</span></div>
<p class="hint">单次最多 100 个链接，重复链接会自动去重。磁力、电驴与直链请分开提交（各自目录不同）。115 对离线下载有配额与会员限制，失败原因以云端返回为准。</p></section>

<section class="panel offline-directory"><div class="section-title"><h2>直链与通用目录</h2><el-tag :type="running? 'warning':'info'">{{runningLabel}}</el-tag><el-tag v-if="panelState('target')" size="small" :type="panelState('target')==='dirty'?'warning':'success'">{{panelState('target')==='dirty'?'有未保存改动':'已保存'}}</el-tag></div>
<p class="selected-directory">{{config.target_label||config.target_cid||'未设置，请选择目录'}}</p>
<div class="button-row"><el-button :disabled="busy" @click="pickerKind='target'">选择保存目录</el-button><el-button v-if="config.target_cid" :disabled="busy" @click="config.target_cid='';config.target_label=''">清除</el-button><el-button type="primary" :disabled="busy||panelState('target')!=='dirty'" @click="savePanel('target')">保存直链目录</el-button></div>
<p class="hint">HTTP/HTTPS/FTP 直链使用这里的目录；未设置时无法提交。不同类型请分开提交。保存任一目录时，会一并保存其他目录和分流的未保存修改。</p>
<div class="organize-option"><label>整理节奏</label>
 <div style="width:100%">
  <el-select v-model="config.scan_minutes" :disabled="busy" aria-label="离线目录整理节奏" style="max-width:220px">
   <el-option :value="0" label="关闭自动整理"/>
   <el-option :value="5" label="每 5 分钟扫一次"/>
   <el-option :value="10" label="每 10 分钟扫一次"/>
   <el-option :value="30" label="每 30 分钟扫一次"/>
  </el-select>
  <p class="hint">手动提交的磁力/电驴下载完成后，定时扫描离线目录里还没整理的新视频并自动整理；关掉后只能手动触发。</p>
 </div>
</div></section>

<section class="panel offline-directory"><div class="section-title"><h2>磁力与种子离线</h2><el-tag v-if="panelState('magnet')" size="small" :type="panelState('magnet')==='dirty'?'warning':'success'">{{panelState('magnet')==='dirty'?'有未保存改动':'已保存'}}</el-tag></div>
<p class="selected-directory">{{config.magnet_label||config.magnet_cid||'未设置，请选择目录'}}</p>
<div class="button-row"><el-button :disabled="busy" @click="pickerKind='magnet'">选择磁力目录</el-button><el-button v-if="config.magnet_cid" :disabled="busy" @click="config.magnet_cid='';config.magnet_label=''">清除</el-button><el-button type="primary" :disabled="busy||panelState('magnet')!=='dirty'" @click="savePanel('magnet')">保存磁力目录</el-button></div>
<div class="organize-option" style="margin-top:10px"><label>关键词分流</label>
 <div style="width:100%">
  <div v-for="(r,ri) in config.magnet_routes" :key="ri" class="offline-route-card">
   <div class="offline-route-head"><strong>分流 {{Number(ri)+1}}</strong><el-button text type="danger" :disabled="busy" @click="dropRoute('magnet',ri)">删除</el-button></div>
   <el-input v-model="r.keywords" aria-label="磁力分流关键词" placeholder="关键词，逗号分隔（如 4K,2160p）" :disabled="busy"/>
   <div class="offline-route-destination"><div><span>保存目录</span><strong>{{r.target_label||r.target_cid||'尚未选择目录'}}</strong></div><el-button :disabled="busy" @click="pickRoute('magnet',ri)">{{r.target_cid?'更换目录':'选择目录'}}</el-button></div>
  </div>
  <el-button size="small" :disabled="busy" @click="addRoute('magnet')">添加分流</el-button>
  <p class="hint">提交文本命中关键词就落到该行目录；都没命中用上面的磁力目录。包含匹配，不是正则。</p>
 </div>
</div>
</section>

<section class="panel offline-directory"><div class="section-title"><h2>ED2K 离线（电驴）</h2><el-tag v-if="panelState('ed2k')" size="small" :type="panelState('ed2k')==='dirty'?'warning':'success'">{{panelState('ed2k')==='dirty'?'有未保存改动':'已保存'}}</el-tag></div>
<p class="selected-directory">{{config.ed2k_label||config.ed2k_cid||'未设置，请选择目录'}}</p>
<div class="button-row"><el-button :disabled="busy" @click="pickerKind='ed2k'">选择 ED2K 目录</el-button><el-button v-if="config.ed2k_cid" :disabled="busy" @click="config.ed2k_cid='';config.ed2k_label=''">清除</el-button><el-button type="primary" :disabled="busy||panelState('ed2k')!=='dirty'" @click="savePanel('ed2k')">保存 ED2K 目录</el-button></div>
<div class="organize-option" style="margin-top:10px"><label>关键词分流</label>
 <div style="width:100%">
  <div v-for="(r,ri) in config.ed2k_routes" :key="ri" class="offline-route-card">
   <div class="offline-route-head"><strong>分流 {{Number(ri)+1}}</strong><el-button text type="danger" :disabled="busy" @click="dropRoute('ed2k',ri)">删除</el-button></div>
   <el-input v-model="r.keywords" aria-label="ED2K 分流关键词" placeholder="关键词，逗号分隔（如 国配,纪录片）" :disabled="busy"/>
   <div class="offline-route-destination"><div><span>保存目录</span><strong>{{r.target_label||r.target_cid||'尚未选择目录'}}</strong></div><el-button :disabled="busy" @click="pickRoute('ed2k',ri)">{{r.target_cid?'更换目录':'选择目录'}}</el-button></div>
  </div>
  <el-button size="small" :disabled="busy" @click="addRoute('ed2k')">添加分流</el-button>
  <p class="hint">ed2k 链接默认落这里；提交文本命中关键词就落到该行目录。频道目录与分流在频道监控页独立配置。</p>
 </div>
</div>
</section>

<section class="panel"><div class="section-title"><h2>云下载任务</h2><el-button :disabled="busy" @click="run(refresh)">刷新</el-button></div>
<el-empty v-if="!tasks.length" :description="total?'已提交，云端任务列表暂未返回内容':'暂无云下载任务'" :image-size="65"/>
<div v-for="(task,i) in tasks" :key="task.id||i" class="job-card"><h3>{{task.name||task.url||'未命名任务'}}</h3><p class="hint">{{statusText(task.status)}} · {{percentText(task.percent)}}<span v-if="task.size"> · {{(Number(task.size)/1024**2).toFixed(0)}} MB</span></p></div>
</section>

<section class="panel" v-if="submissions.length"><div class="section-title"><h2>提交记录（含本批整理进度）</h2><el-button size="small" :disabled="busy" @click="expireOld">放弃 7 天前的待核查</el-button></div>
<div v-for="s in submissions" :key="s.id" class="job-card">
<h3>#{{s.id}} · {{s.target_label||s.target_cid}} · {{fmtSub(s.submitted_at)}}</h3>
<p class="hint">
<el-tag size="small" :type="s.state==='completed'?'success':s.state==='submitted'?'info':'warning'">{{subState(s.state)}}</el-tag>
　{{s.message||'—'}}<span v-if="s.organize_job_id">（整理任务 <b>#{{s.organize_job_id}}</b>，见任务中心）</span></p>
</div>
</section>

<DirectoryPicker v-if="pickerKind" :api="api" @close="()=>{pickerKind='';routePick=null}" @select="onPicked"/>
</template>

<style scoped>

.offline-directory .selected-directory{border:1px solid var(--line);padding:13px 15px;font-weight:600;color:var(--text)!important}.offline-route-row{display:flex;align-items:center;gap:8px;margin-bottom:8px;min-width:0}.offline-route-row :deep(.el-input){max-width:260px;min-width:0;flex:1}.offline-route-row :deep(.el-button){margin:0;max-width:45%;overflow:hidden;text-overflow:ellipsis}.offline-submit :deep(.el-textarea__inner){padding:14px 16px;background:var(--surface-soft);line-height:1.8}.offline-directory .organize-option{border-top:1px solid var(--line);padding-top:15px}.offline-directory .hint{line-height:1.7}
.offline-destination-label{margin:0 0 8px;font-size:12px}.offline-destinations{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0 0 16px}.offline-destinations>div{min-width:0;background:var(--surface-soft);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.offline-destinations span,.offline-destinations strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.offline-destinations span{font-size:11px;color:var(--muted);margin-bottom:5px}.offline-destinations strong{font-size:12px;color:var(--text)}
.offline-route-card{padding:14px;margin-bottom:10px;border:1px solid var(--line);border-radius:9px;background:var(--surface-soft)}.offline-route-head,.offline-route-destination{display:flex;justify-content:space-between;align-items:center;gap:12px}.offline-route-head{margin-bottom:10px}.offline-route-head strong,.offline-route-destination strong{font-size:13px;color:var(--text)}.offline-route-head :deep(.el-button),.offline-route-destination :deep(.el-button){flex:none;margin:0}.offline-route-destination{margin-top:12px}.offline-route-destination>div{min-width:0;flex:1}.offline-route-destination span,.offline-route-destination strong{display:block}.offline-route-destination span{color:var(--muted);font-size:11px;margin-bottom:4px}.offline-route-destination strong{line-height:1.5;overflow-wrap:anywhere}
@media(max-width:560px){.offline-destinations{grid-template-columns:1fr;gap:6px}.offline-destinations>div{display:flex;align-items:center;gap:10px;padding:9px 10px}.offline-destinations span{margin:0;flex:none}.offline-destinations strong{min-width:0}}
@media(max-width:760px){.offline-route-row{flex-wrap:wrap}.offline-route-row :deep(.el-input){flex:1 0 100%;max-width:none}.offline-route-row :deep(.el-button){max-width:100%;min-width:0}.offline-directory .button-row{gap:8px}.offline-submit :deep(.el-textarea__inner){min-height:140px}}
</style>
