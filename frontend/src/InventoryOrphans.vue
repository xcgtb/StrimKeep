<script setup lang="ts">
import {onMounted,onUnmounted,ref} from 'vue'
import {ElMessageBox} from 'element-plus'

const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const orphanBusy=ref(false),orphanError=ref(''),cloudOrphanError=ref(''),orphanResult=ref<any>(null),orphanSelected=ref<any[]>([])
const orphanTable=ref<any>(null)
const orphanJobs=ref<any[]>([]),orphanJob=ref<any>(null)
let orphanTimer:ReturnType<typeof setTimeout>|undefined
const skipReasons:Record<string,string>={symlink:'软链接目录',has_media_or_unknown:'含视频/未知文件',
  changed:'扫描时变动',too_large:'过大过深',not_plain_directory:'不是普通目录',no_tmdb:'无 TMDB 标识',
  unreadable:'读取失败',invalid:'配置不合法'}
function skipReason(code:string){return skipReasons[code]||code}
function side(value:string){return value==='cloud'?'网盘库':value==='share'?'分享库':value==='cloud115'?'115 网盘':'待核实'}
function jobState(state:string){return ({queued:'等待执行',running:'清理中',completed:'已完成',partial:'部分完成',interrupted:'已中断'} as Record<string,string>)[state]||state}
async function scanOrphans(){
  orphanBusy.value=true;orphanError.value='';cloudOrphanError.value='';orphanResult.value=null;orphanSelected.value=[]
  try{
    const local=await props.api('/inventory/orphans/scan','POST')
    let remote:any={items:[],skipped:0}
    try{remote=await props.api('/inventory/cloud-orphans/scan','POST')}
    catch(e){cloudOrphanError.value='115 网盘扫描未完成，本次只能清理本地：'+(e as Error).message}
    const byPath=new Map<string,any>(remote.items.map((row:any)=>[row.local_path,row]))
    const linked=new Set<string>()
    const rows=local.items.map((row:any)=>{const found=row.side==='cloud'&&row.kind!=='file'?byPath.get(row.path):null;if(found){linked.add(found.id);return {...row,linkedCloud:found}}return row})
    for(const row of remote.items)if(!linked.has(row.id))rows.push({side:'cloud115',kind:'cloud_directory',path:row.path,files:row.files,dirs:row.dirs,linkedCloud:row})
    orphanResult.value={...local,items:rows,cloudAttentionCount:remote.attention_count||0,cloudAttentionItems:remote.attention_items||[]}
  }catch(e){orphanError.value=(e as Error).message}
  finally{orphanBusy.value=false}
}
async function loadOrphanJobs(){
  try{
    const response=await props.api('/inventory/orphans/jobs')
    orphanJobs.value=response.items||[]
    const active=orphanJobs.value.find((job:any)=>['queued','running'].includes(job.state))
    if(active){orphanJob.value=active;orphanBusy.value=true;scheduleOrphanPoll()}
    else{if(orphanJob.value)orphanJob.value=orphanJobs.value.find((job:any)=>job.id===orphanJob.value.id)||null;orphanBusy.value=false}
  }catch(e){orphanError.value='清理记录读取失败：'+(e as Error).message;if(orphanBusy.value)scheduleOrphanPoll()}
}
function scheduleOrphanPoll(){if(orphanTimer)clearTimeout(orphanTimer);orphanTimer=setTimeout(loadOrphanJobs,3000)}
async function cleanOrphans(){
  if(!orphanSelected.value.length)return
  const cloudCount=orphanSelected.value.filter(row=>row.linkedCloud).length
  try{await ElMessageBox.confirm(`将清理选中的 ${orphanSelected.value.length} 项；其中 ${cloudCount} 项会联动回收 115 网盘的字幕、元数据和空目录。分享侧只清理本地。执行前会再次核对。`,'确认清理残留',{type:'warning',confirmButtonText:'确认清理',cancelButtonText:'取消'})}catch{return}
  orphanBusy.value=true;orphanError.value=''
  const id=Array.from(crypto.getRandomValues(new Uint8Array(16))).map(x=>x.toString(16).padStart(2,'0')).join('')
  const items=orphanSelected.value.map(row=>({
    local:row.kind==='cloud_directory'?null:{path:row.path,fingerprint:row.fingerprint,kind:row.kind||'directory',source:row.source||''},
    cloud:row.linkedCloud?{id:row.linkedCloud.id,fingerprint:row.linkedCloud.fingerprint}:null
  }))
  orphanResult.value=null;orphanSelected.value=[]
  try{orphanJob.value=await props.api('/inventory/orphans/jobs','POST',{id,items});await loadOrphanJobs()}
  catch(e){
    try{orphanJob.value=await props.api('/inventory/orphans/jobs/'+id);await loadOrphanJobs()}
    catch{orphanBusy.value=false;orphanError.value='提交状态无法确认，请刷新清理记录并重新扫描后再操作：'+(e as Error).message}
  }
}
onMounted(loadOrphanJobs)
onUnmounted(()=>{if(orphanTimer)clearTimeout(orphanTimer)})
</script>
<template>
 <div class="inventory-orphans">
  <section class="orphan-hero"><div><h2>目录残留清理</h2><p>扫描本地两库和 115 归档目录</p></div><div class="orphan-actions"><el-button type="primary" :loading="orphanBusy" @click="scanOrphans">扫描残留</el-button><el-button type="danger" :loading="orphanBusy" :disabled="!orphanSelected.length || orphanSelected.length > 100" @click="cleanOrphans">清理选中（{{orphanSelected.length}}）</el-button></div><details class="inventory-help orphan-help"><summary>清理范围与规则</summary><p>网盘侧同一作品可联动清理本地残留及 115 字幕、元数据和空目录；分享侧只清理本地。只处理带 TMDB 标识且没有视频或未知文件的目录；115 文件进入回收站，每次最多 100 项。</p></details></section>
  <el-alert v-if="orphanError" :title="orphanError" type="error" :closable="false" /><el-alert v-if="cloudOrphanError" :title="cloudOrphanError" type="warning" :closable="false" />
  <template v-if="orphanResult"><p>发现 {{ orphanResult.items.length }} 个可清理残留项</p>
  <details v-if="orphanResult.attention_count || orphanResult.cloudAttentionCount" class="orphan-attention">
   <summary>有 {{ (orphanResult.attention_count || 0) + (orphanResult.cloudAttentionCount || 0) }} 项需要核对 <span>展开查看</span></summary>
   <p class="muted">以下目录暂不清理，请核对原因；含正常视频或 STRM 的目录已自动保留，不在此显示。</p>
   <p v-for="(row,i) in orphanResult.attention_items" :key="'local'+i" class="file-path">{{ side(row.side) }} · {{ row.path }}<br>{{ skipReason(row.reason) }}<span v-if="row.detail">：{{row.detail}}</span></p>
   <p v-for="(row,i) in orphanResult.cloudAttentionItems" :key="'cloud'+i" class="file-path">115 网盘 · {{row.path || row.id}}<br>{{row.reason}}</p>
   <p v-if="(orphanResult.attention_count || 0) > (orphanResult.attention_items?.length || 0) || orphanResult.cloudAttentionCount > orphanResult.cloudAttentionItems.length" class="muted">仅显示部分条目。</p>
  </details>

  <el-table ref="orphanTable" class="orphan-desktop" :data="orphanResult.items" @selection-change="orphanSelected = $event"><el-table-column type="expand"><template #default="s"><div v-if="s.row.linkedCloud"><strong>115 网盘待回收附属文件</strong><p v-for="(name,i) in s.row.linkedCloud.file_names" :key="i" class="file-path">{{name}}</p><p v-if="s.row.linkedCloud.files>s.row.linkedCloud.file_names.length" class="muted">仅显示前 {{s.row.linkedCloud.file_names.length}} 个文件；本项共 {{s.row.linkedCloud.files}} 个。</p><p v-if="!s.row.linkedCloud.files" class="muted">目录已空，无附属文件。</p></div><p v-else class="muted">只清理本地残留。</p></template></el-table-column><el-table-column type="selection" width="45" /><el-table-column label="库" width="90"><template #default="s">{{ side(s.row.side) }}<span v-if="s.row.linkedCloud&&s.row.side==='cloud'"> + 115</span></template></el-table-column><el-table-column label="类型" width="100"><template #default="s">{{ s.row.kind === 'file' ? '附属文件' : '作品目录' }}</template></el-table-column><el-table-column prop="path" label="路径" min-width="240" /><el-table-column prop="files" label="附属文件" width="100" /><el-table-column prop="dirs" label="目录" width="70" /></el-table><div class="orphan-mobile"><article v-for="row in orphanResult.items" :key="row.side+row.kind+row.path" class="orphan-card"><div class="orphan-card-head"><el-checkbox :model-value="orphanSelected.includes(row)" :aria-label="`选择 ${row.path}`" @change="(v:any)=>orphanTable?.toggleRowSelection(row,Boolean(v))"/><strong>{{side(row.side)}}<span v-if="row.linkedCloud&&row.side==='cloud'"> + 115</span> · {{row.kind==='file'?'附属文件':'作品目录'}}</strong><span>{{row.files}} 文件 · {{row.dirs}} 目录</span></div><p class="file-path">{{row.path}}</p><details v-if="row.linkedCloud"><summary>查看 115 附属文件（{{row.linkedCloud.files}}）</summary><p v-for="(name,i) in row.linkedCloud.file_names" :key="i" class="file-path">{{name}}</p><p v-if="row.linkedCloud.files>row.linkedCloud.file_names.length" class="muted">仅显示前 {{row.linkedCloud.file_names.length}} 个文件。</p></details><p v-else class="muted">只清理本地残留</p></article></div></template>

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
 </div>
</template>

<style scoped>
.file-path{font-size:12px;word-break:break-all;color:var(--el-text-color-secondary)}
.inventory-help{padding:11px 14px;border:1px solid var(--el-border-color-lighter);border-radius:10px;background:var(--el-bg-color);font-size:12px;line-height:1.6;color:var(--el-text-color-secondary)}.inventory-help summary{cursor:pointer;color:var(--el-text-color-regular);font-size:13px;font-weight:600}.inventory-help p{margin:8px 0 0}
.orphan-attention{margin:12px 0;padding:12px;border:1px solid var(--el-border-color-lighter);border-radius:9px}.orphan-attention summary{cursor:pointer}.orphan-attention summary span{float:right;color:var(--el-text-color-secondary);font-size:12px}
.orphan-progress,.orphan-history{margin-top:18px;padding:16px;border:1px solid var(--el-border-color-lighter);border-radius:12px;background:var(--el-fill-color-blank)}
.orphan-progress p{margin:10px 0}.orphan-progress .el-progress{margin-top:12px}.orphan-history>summary,.orphan-job>summary{display:flex;justify-content:space-between;gap:12px;cursor:pointer;align-items:center}.orphan-history>summary span,.orphan-job summary span:last-child{color:var(--el-text-color-secondary);font-size:12px}.orphan-job{margin-top:12px;padding:12px;border:1px solid var(--el-border-color-lighter);border-radius:9px}.orphan-job summary strong,.orphan-job summary small{display:block}.orphan-job summary small{margin-top:5px;color:var(--el-text-color-secondary)}.orphan-record{padding:12px 0;border-top:1px solid var(--el-border-color-lighter);margin-top:12px;font-size:13px}.orphan-record p{margin:7px 0}.orphan-error{color:var(--el-color-danger);font-size:12px;word-break:break-word}
.orphan-hero{padding:16px 18px;border:1px solid var(--el-border-color-lighter);border-radius:12px;background:var(--el-bg-color);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:14px}.orphan-hero h2{margin:0 0 5px;font-size:17px}.orphan-hero>div>p{margin:0;font-size:12px;color:var(--el-text-color-secondary)}.orphan-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px}.orphan-help{flex-basis:100%;background:transparent}.orphan-mobile{display:none}.orphan-card{padding:12px;border:1px solid var(--el-border-color-lighter);border-radius:10px;background:var(--el-bg-color)}.orphan-card-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.orphan-card-head strong{font-size:13px}.orphan-card-head span{margin-left:auto;font-size:12px;color:var(--el-text-color-secondary)}.orphan-card p{margin:8px 0;overflow-wrap:anywhere}.orphan-card details{font-size:12px}.orphan-card details summary{cursor:pointer;color:var(--el-color-primary)}
@media(max-width:639px){.orphan-hero{padding:14px}.orphan-actions{width:100%}.orphan-actions :deep(.el-button){flex:1;padding-left:8px;padding-right:8px}.orphan-desktop{display:none}.orphan-mobile{display:grid;gap:8px}}
</style>

