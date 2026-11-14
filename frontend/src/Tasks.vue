<script setup lang="ts">
import {computed,ref,shallowRef} from 'vue'
import {ElMessageBox} from 'element-plus'
import {mergeTaskDetail} from './taskDetails'
import {taskPresentation,taskStatus} from './taskPresentation'
import {usePageActivity} from './pageActivity'
import {reconcileRows} from './taskRows'
import {taskBucket} from './taskBuckets'
import DeepDeleteHistory from './DeepDeleteHistory.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>,embedded?:boolean}>()
const active=shallowRef<any[]>([]),history=shallowRef<any[]>([]),detail=shallowRef<any>(null),busy=ref(false),error=ref(''),live=ref(false),more=ref(false)
const kinds:Record<string,string>={transfer:'115 转存',organize:'扫描整理',archive_index:'归档索引',strm_sync:'STRM 同步',classification_migration:'历史分类迁移（已停用）'}
let stream:EventSource|undefined,timer:ReturnType<typeof setInterval>|undefined,polling=false,visible=false
const inProgress=(job:any)=>['queued','running','paused','waiting_login'].includes(job.state)
function taskSource(job:any){
  const config=job.config||{},origin=String(config.origin||'')
  if(origin.startsWith('offline:'))return '离线下载'
  if(config.transfer_parent)return '转存后整理'
  if(origin==='channel')return '频道监控'
  if(origin==='telegram')return 'Telegram'
  return config.scheduled?'定时':'手动'
}
const tab=ref('all')
// 分类按「结果」而不是按任务类型：用户关心的是这条到底成没成、卡在哪。
const bucketOf=taskBucket
const allJobs=computed(()=>[...active.value,...history.value])
const matchTab=(job:any)=>tab.value==='all'||bucketOf(job)===tab.value
const shownActive=computed(()=>active.value.filter(matchTab))
const shownHistory=computed(()=>history.value.filter(matchTab))
const bucketCount=(bucket:string)=>allJobs.value.filter(job=>bucketOf(job)===bucket).length
async function load(older=false){const before=older&&history.value.length?'?before='+history.value.at(-1).id:'';const data=await props.api('/tasks'+before);active.value=reconcileRows(active.value,data.active);history.value=reconcileRows(history.value,older?[...history.value,...data.history]:data.history);more.value=data.has_more}
async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function fmtAgo(ts:number){if(!ts)return '';const diff=Date.now()/1000-ts;if(diff<60)return '刚刚';if(diff<3600)return Math.floor(diff/60)+' 分钟前';if(diff<86400)return Math.floor(diff/3600)+' 小时前';const d=new Date(ts*1000);return `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`}
async function control(job:any,action:string){if(action==='cancel'){try{await ElMessageBox.confirm('取消后保留已完成步骤，在途操作会先核查结果。','取消任务')}catch{return}}await run(async()=>{await props.api(`/organize/jobs/${job.id}/${action}`,'POST');await load()})}
async function show(id:number,after=0){await run(async()=>{const data=await props.api(`/organize/jobs/${id}?after=${after}`);if(after&&detail.value?.job.id===id)data.items=[...detail.value.items,...data.items];detail.value=data})}
function connect(){stream?.close();stream=new EventSource('/api/v1/tasks/events');stream.onopen=()=>live.value=true;stream.addEventListener('tasks',event=>{if(!visible)return;try{const items=JSON.parse((event as MessageEvent).data).items;active.value=reconcileRows(active.value,items.filter(inProgress).sort((a:any,b:any)=>a.id-b.id));const incoming=items.filter((j:any)=>!inProgress(j));const ids=new Set(items.map((j:any)=>j.id));history.value=reconcileRows(history.value,[...incoming,...history.value.filter(j=>!ids.has(j.id))].sort((a,b)=>b.id-a.id));live.value=true}catch{live.value=false}});stream.onerror=()=>live.value=false}
async function poll(){
  if(!visible||polling||busy.value)return
  polling=true
  try{
    if(!live.value)await load()
    const current=detail.value
    if(current){
      const data=await props.api(`/organize/jobs/${current.job.id}`)
      // The drawer may have closed or switched while the request was pending.
      detail.value=mergeTaskDetail(detail.value,current,data)
    }
    error.value=''
  }catch(e){error.value=(e as Error).message}finally{polling=false}
}
usePageActivity(()=>{visible=true;connect();void run(()=>load());timer=setInterval(poll,5000)},()=>{visible=false;live.value=false;stream?.close();stream=undefined;if(timer)clearInterval(timer);timer=undefined})
</script>
<template>
<div v-if="!embedded" class="page-title"><div><h1>任务中心</h1><p class="muted">查看进行中和已结束的任务，跟踪处理结果。</p></div></div>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>
<section class="panel tasks-panel">
  <div class="section-title"><h2>任务</h2><el-button v-if="tab!=='deep-delete'" :disabled="busy" @click="run(()=>load())">刷新</el-button></div>
  <el-tabs v-model="tab">
    <el-tab-pane :label="`全部 ${allJobs.length}`" name="all"/>
    <el-tab-pane :label="`进行中 ${bucketCount('progress')}`" name="progress"/>
    <el-tab-pane :label="`完成 ${bucketCount('done')}`" name="done"/>
    <el-tab-pane :label="`过滤 ${bucketCount('filtered')}`" name="filtered"/>
    <el-tab-pane :label="`未识别 ${bucketCount('unidentified')}`" name="unidentified"/>
    <el-tab-pane :label="`需处理 ${bucketCount('failed')}`" name="failed"/>
    <el-tab-pane :label="`已取消 ${bucketCount('cancelled')}`" name="cancelled"/>
    <el-tab-pane label="深度删除" name="deep-delete"/>
  </el-tabs>
  <DeepDeleteHistory v-if="tab==='deep-delete'" :api="api"/>
  <template v-else>
  <p v-if="more" class="hint">数量仅包含已载入的任务；可在下方载入更早记录。</p>
  <h2>进行中 <span class="task-count">{{shownActive.length}}</span></h2>
  <p class="hint">整理、转存和 STRM 同步分别处理；同类任务按顺序执行，访问同一目录时可能短暂等待。暂停、等待登录或失败的批次不阻塞后续任务。</p>
  <el-empty v-if="!shownActive.length" :description="tab==='all'?'当前没有进行中的任务':'此分类没有进行中的任务'" :image-size="64"/>
  <div v-for="(job,i) in shownActive" :key="job.id" v-memo="[job,busy]" class="job-card">
    <p class="hint"><span :title="new Date(job.updated*1000).toLocaleString()">{{fmtAgo(job.updated)}}</span> · #{{job.id}} · {{kinds[job.kind]||job.kind}} · {{taskSource(job)}}任务
      <el-tag :type="taskStatus(job).type" size="small">{{taskStatus(job).label}}</el-tag></p>
    <h3>{{taskPresentation(job).title}}</h3>
    <el-progress v-if="['queued','running'].includes(job.state)&&job.progress!==undefined" :percentage="job.progress"/>
    <p v-if="job.state==='running'" class="wrap">{{job.stage}}</p>
    <p v-if="job.manifest&&job.total" class="hint">已处理 {{job.finished}} / {{job.total}}</p>
    <p v-for="note in taskPresentation(job).notes" :key="note" class="wrap">{{note}}</p>
    <div class="button-row">
      <el-button :disabled="busy" @click="show(job.id)">查看处理结果</el-button>
      <el-button v-if="['queued','running','waiting_login'].includes(job.state)" :disabled="busy" @click="control(job,'pause')">暂停</el-button>
      <el-button v-if="['paused','waiting_login','needs_review'].includes(job.state)" :disabled="busy" @click="control(job,'resume')">{{job.state==='needs_review'?'核查并继续':'继续任务'}}</el-button>
      <el-button type="danger" plain :disabled="busy" @click="control(job,'cancel')">取消</el-button>
    </div>
  </div>
  <h2>已结束 <span class="task-count">{{shownHistory.length}}</span></h2>
  <el-empty v-if="!shownHistory.length" description="这个分类下还没有已结束的任务" :image-size="64"/>
  <div v-for="job in shownHistory" :key="job.id" v-memo="[job,busy]" class="job-card">
    <p class="hint"><span :title="new Date(job.updated*1000).toLocaleString()">{{fmtAgo(job.updated)}}</span> · #{{job.id}} · {{kinds[job.kind]||job.kind}} · {{taskSource(job)}}任务
      <el-tag :type="taskStatus(job).type" size="small">{{taskStatus(job).label}}</el-tag></p>
    <h3>{{taskPresentation(job).title}}</h3>
    <p v-if="job.state==='needs_review'&&job.message" class="wrap">失败原因：{{job.message}}</p>
    <p v-if="taskPresentation(job).summary" class="wrap">{{taskPresentation(job).summary}}</p>
    <p v-for="note in taskPresentation(job).notes" :key="note" class="wrap">{{note}}</p>
    <div class="button-row">
      <el-button v-if="job.total||job.state!=='completed'" :disabled="busy" @click="show(job.id)">查看处理结果</el-button>
      <el-button v-if="job.state==='needs_review'" :disabled="busy" @click="control(job,'resume')">核查并继续</el-button>
      <el-button v-if="job.state==='needs_review'" type="danger" plain :disabled="busy" @click="control(job,'cancel')">归档这条失败任务</el-button>
      <el-button v-if="['partial','cancelled'].includes(job.state)" plain :disabled="busy" @click="control(job,'close')">结项</el-button>
    </div>
    <details v-if="job.kind==='organize'&&['completed','partial','cancelled'].includes(job.state)&&(job.counts.done||job.counts.archived||job.can_retry_cleanup||job.can_recheck_cleanup)" class="task-extra">
      <summary>更多操作</summary>
      <p class="hint">STRM 或字幕缺失时，可重新生成已归档文件的本地输出。</p>
      <el-button v-if="job.counts.done||job.counts.archived" :disabled="busy" @click="control(job,'retry-output')">重新生成 STRM 与字幕</el-button>
      <el-button v-if="job.can_retry_cleanup" :disabled="busy" @click="control(job,'retry-cleanup')">重试空目录清理</el-button>
      <el-button v-if="job.can_recheck_cleanup" :disabled="busy" @click="control(job,'recheck-cleanup')">重新核查目录残留</el-button>

      <p v-if="job.can_retry_cleanup||job.can_recheck_cleanup" class="hint">目录清理会回收空目录、海报/NFO 和本次扫描中无法跟随且身份已核实的字幕；视频、未识别资源及有歧义的内容会保留。</p>
    </details>
  </div>
  <el-button v-if="more" :disabled="busy" @click="run(()=>load(true))">更早的任务</el-button>
  </template>
</section>
<el-drawer :model-value="!!detail" :title="detail?`任务 #${detail.job.id} 处理结果`:'处理结果'" size="min(900px, 100%)" @close="detail=null">
  <template v-if="detail">
    <h3>{{taskPresentation(detail.job).title}}</h3>
    <p class="wrap">当前阶段：{{detail.job.stage}}</p>
    <p v-if="taskPresentation(detail.job).summary" class="wrap">{{taskPresentation(detail.job).summary}}</p>
    <p v-for="note in taskPresentation(detail.job).notes" :key="note" class="wrap">{{note}}</p>
    <details v-if="detail.job.message" class="task-extra"><summary>技术详情</summary><p class="wrap">{{detail.job.message}}</p></details>
    <div v-for="item in detail.items" :key="item.id" class="job-card">
      <h3 class="wrap">{{item.original_name}}</h3><el-tag>{{item.step}}</el-tag>
      <p v-if="item.target_name" class="wrap">{{item.target_name}}</p>
      <p class="hint wrap">{{item.relative_path}}</p>
      <p v-if="item.message" class="wrap">{{item.message}}</p>
      <details class="task-extra"><summary>文件与洗版详情</summary>
        <p>文件 ID {{item.file_id}}</p>
        <p v-if="item.cloud_parts?.length">115 归档：{{item.cloud_parts.join(' / ')}}</p>
        <p v-if="item.tmdb_id">TMDB {{item.tmdb_id}} · {{item.media?.width}} × {{item.media?.height}} · {{item.media?.codec}}</p>
        <p v-if="item.upgrade?.decision">洗版决定：{{({replace:'更优替换',discard:'回收新副本',archive:'归档或共存',skip:'原位置跳过'} as Record<string,string>)[item.upgrade.decision]||item.upgrade.decision}}</p>
        <div v-for="check in item.upgrade?.comparisons||[]" :key="check.file_id"><p>比较旧文件 {{check.file_id}}：{{check.result==='identical'?'内容相同':check.result==='coexist'?'分组共存':check.result>0?'新版更优':check.result<0?'旧版更优':'同分'}}</p><p v-for="rule in check.rules" :key="rule.rule">{{rule.label}}：新 {{JSON.stringify(rule.new)}} / 旧 {{JSON.stringify(rule.old)}}</p></div>
        <p v-for="action in item.recycle||[]" :key="action.file_id">回收文件 {{action.file_id}}：{{action.state==='confirmed'?'已核实':'意图已保存，待核实'}}</p>
      </details>
    </div>
    <el-button v-if="detail.has_more" :disabled="busy" @click="show(detail.job.id,detail.items.at(-1).id)">后续文件</el-button>
  </template>
</el-drawer>
</template>

<style scoped>

.tasks-panel :deep(.el-tabs__header){margin-bottom:22px}.tasks-panel>.hint{margin:0 0 12px}.tasks-panel>.job-card{border-left:3px solid color-mix(in srgb,var(--accent) 38%,var(--line))!important}.tasks-panel>.job-card h3{font-size:14px}.tasks-panel>.job-card .hint{line-height:1.6}.tasks-panel :deep(.el-empty){padding:20px 0}.tasks-panel :deep(.el-progress-bar__outer){background:var(--surface-soft)}.task-count{display:inline-block;min-width:20px;margin-left:5px;padding:2px 6px;border-radius:10px;background:var(--surface-soft);color:var(--muted);font-size:11px;text-align:center;vertical-align:middle}
@media(max-width:760px){.tasks-panel :deep(.el-tabs__item){padding:0 12px}.tasks-panel>.job-card{padding:13px}.tasks-panel :deep(.el-empty){padding:16px 0}}
</style>
