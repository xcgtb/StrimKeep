<script setup lang="ts">
import PageHeader from './PageHeader.vue'
import WorkspaceSections from './WorkspaceSections.vue'
import {computed,ref,shallowRef} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import {mergeTaskDetail} from './taskDetails'
import {taskPresentation,taskStatus} from './taskPresentation'
import {usePageActivity} from './pageActivity'
import {reconcileRows} from './taskRows'
import {taskBucket} from './taskBuckets'
import {taskKindLabel, taskSourceLabel} from './taskView'
import DeepDeleteHistory from './DeepDeleteHistory.vue'
import ManualMatch from './ManualMatch.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>,embedded?:boolean}>()
const active=shallowRef<any[]>([]),history=shallowRef<any[]>([]),detail=shallowRef<any>(null),busy=ref(false),error=ref(''),live=ref(false),more=ref(false)
type OfflineRecord={id:number,target_label:string,submitted_at:number,state:string,message:string}
type OfflineSection={total:number,items:OfflineRecord[]}
const offlinePending=ref<OfflineSection>({total:0,items:[]})
const offlineReview=ref<OfflineSection>({total:0,items:[]})
const offlineStatusError=ref('')
const syncStageLabels = {
  connection:'115 连接检查', scan_wait:'等待扫描通道', scan:'115 目录枚举',
  verify:'文件核对', publish_wait:'等待发布通道', publish:'本地写入',
  local_scan:'本地扫描', local_cleanup:'失效核对与清理',
  sidecars:'附属文件', technical:'技术媒体信息', emby:'Emby 通知提交',
} as const
const syncListings: Record<string, string> = {bulk:'批量目录树',recursive:'递归文件列表',directory:'逐目录读取'}
const seconds = (value: number) => value.toFixed(2) + ' 秒'
const syncMetrics = computed(() => detail.value?.job.sync_metrics)
const syncStages = computed(() => Object.entries(syncStageLabels)
  .flatMap(([key,label]) => {
    const value=syncMetrics.value?.stages?.[key]
    return typeof value==='number' ? [{key,label,value}] : []
  }))
let stream:EventSource|undefined,timer:ReturnType<typeof setInterval>|undefined,polling=false,visible=false
const inProgress=(job:any)=>['queued','running','paused','waiting_login'].includes(job.state)
const taskSource=taskSourceLabel
const kinds=new Proxy({} as Record<string,string>,{get(_t,key:string){return taskKindLabel(key)}})
const tab=ref('all')
// 分类按「结果」而不是按任务类型：用户关心的是这条到底成没成、卡在哪。
const bucketOf=taskBucket
const allJobs=computed(()=>[...active.value,...history.value])
const matchTab=(job:any)=>tab.value==='all'||bucketOf(job)===tab.value
const shownActive=computed(()=>active.value.filter(matchTab))
const shownHistory=computed(()=>history.value.filter(matchTab))
const bucketCount=(bucket:string)=>allJobs.value.filter(job=>bucketOf(job)===bucket).length
  +(bucket==='progress'?offlinePending.value.total:bucket==='failed'?offlineReview.value.total:0)
const taskSections = computed(() => [
  {id:'all', label:'全部 ' + (allJobs.value.length + offlinePending.value.total)},
  {id:'progress', label:'进行中 ' + bucketCount('progress')},
  {id:'done', label:'完成 ' + bucketCount('done')},
  {id:'filtered', label:'过滤 ' + bucketCount('filtered')},
  {id:'unidentified', label:'未识别 ' + bucketCount('unidentified')},
  {id:'failed', label:'需处理 ' + bucketCount('failed')},
  {id:'cancelled', label:'已取消 ' + bucketCount('cancelled')},
  {id:'deep-delete', label:'深度删除'},
])
const shownProgressCount=computed(()=>shownActive.value.length+offlinePending.value.total)
const itemFilter=ref('all')
const manualItem=ref<any>(null)
const itemText=(item:any)=>`${item.step||''} ${item.message||''}`
const isUnidentified=(item:any)=>item.unidentified===true
const canHandleUnidentified=computed(()=>detail.value&&!['queued','running','paused','waiting_login'].includes(detail.value.job.state))
const canManualMatch=computed(()=>canHandleUnidentified.value&&(detail.value?.job.config?.job_kind||'organize')==='organize')
const isFailedItem=(item:any)=>item.state==='review'||/失败|异常|未完成/.test(itemText(item))
const isDoneItem=(item:any)=>['done','skipped','archived','recycled'].includes(item.state)
const unidentifiedItems=computed(()=>detail.value?.items.filter(isUnidentified)||[])
const failedItems=computed(()=>detail.value?.items.filter(isFailedItem)||[])
const doneItems=computed(()=>detail.value?.items.filter(isDoneItem)||[])
const detailItems=computed(()=>{const items=detail.value?.items||[];if(itemFilter.value==='unidentified')return items.filter(isUnidentified);if(itemFilter.value==='failed')return items.filter(isFailedItem);if(itemFilter.value==='done')return items.filter(isDoneItem);return items})
async function loadOffline(){
  try {
    const result=await props.api('/offline/submissions/attention')
    offlinePending.value=result.active
    offlineReview.value=result.review
    offlineStatusError.value=''
  } catch {
    offlineStatusError.value='离线下载状态暂时无法读取'
  }
}
async function load(older=false){const before=older&&history.value.length?'?before='+history.value.at(-1).id:'';const data=await props.api('/tasks'+before);active.value=reconcileRows(active.value,data.active);history.value=reconcileRows(history.value,older?[...history.value,...data.history]:data.history);more.value=data.has_more;await loadOffline()}
async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function fmtAgo(ts:number){if(!ts)return '';const diff=Date.now()/1000-ts;if(diff<60)return '刚刚';if(diff<3600)return Math.floor(diff/60)+' 分钟前';if(diff<86400)return Math.floor(diff/3600)+' 小时前';const d=new Date(ts*1000);return `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`}
async function control(job:any,action:string){if(action==='cancel'){try{await ElMessageBox.confirm('取消后保留已完成步骤，在途操作会先核查结果。','取消任务')}catch{return}}await run(async()=>{await props.api(`/organize/jobs/${job.id}/${action}`,'POST');await load()})}
async function show(id:number,after=0){if(!after)itemFilter.value='all';await run(async()=>{const data=await props.api(`/organize/jobs/${id}?after=${after}`);if(after&&detail.value?.job.id===id)data.items=[...detail.value.items,...data.items];detail.value=data})}
async function copyName(name:string){if(!navigator.clipboard){ElMessage.warning('浏览器未允许复制，请手动选择文件名。');return}try{await navigator.clipboard.writeText(name);ElMessage.success('文件名已复制，可粘贴到「识别测试」核对。')}catch{ElMessage.warning('复制失败，请手动选择文件名。')}}
async function dismissUnrecognized(itemIds:number[]=[]){const current=detail.value;if(!current)return;const jobId=current.job.id;try{await ElMessageBox.confirm('只把未识别项标记为已跳过，不删除文件；文件仍在原目录或未识别目录。其他失败项不会被忽略。','人工处理未识别项',{confirmButtonText:'确认忽略',cancelButtonText:'取消'})}catch{return}await run(async()=>{const result=await props.api(`/organize/jobs/${jobId}/dismiss-unrecognized`,'POST',{item_ids:itemIds});ElMessage.success(`已忽略 ${result.dismissed} 项${result.closed?'，任务已结项':''}`);detail.value=await props.api(`/organize/jobs/${jobId}`);await load()})}
async function manualCreated(){manualItem.value=null;await run(async()=>{if(detail.value)detail.value=await props.api(`/organize/jobs/${detail.value.job.id}`);await load()})}
function connect(){stream?.close();stream=new EventSource('/api/v1/tasks/events');stream.onopen=()=>live.value=true;stream.addEventListener('tasks',event=>{if(!visible)return;try{const items=JSON.parse((event as MessageEvent).data).items;active.value=reconcileRows(active.value,items.filter(inProgress).sort((a:any,b:any)=>a.id-b.id));const incoming=items.filter((j:any)=>!inProgress(j));const ids=new Set(items.map((j:any)=>j.id));history.value=reconcileRows(history.value,[...incoming,...history.value.filter(j=>!ids.has(j.id))].sort((a,b)=>b.id-a.id));live.value=true}catch{live.value=false}});stream.onerror=()=>live.value=false}
async function poll(){
  if(!visible||polling||busy.value)return
  polling=true
  try{
    if(!live.value)await load();else await loadOffline()
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
<PageHeader v-if="!embedded" title="任务中心">
  <template v-if="tab!=='deep-delete'" #actions><el-button :disabled="busy" @click="run(()=>load())">刷新</el-button></template>
</PageHeader>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>
<section class="panel tasks-panel">
  <div v-if="embedded" class="section-title"><h2>任务</h2><el-button v-if="tab!=='deep-delete'" :disabled="busy" @click="run(()=>load())">刷新</el-button></div>
  <WorkspaceSections v-model="tab" label="任务状态与记录" :items="taskSections" class="task-sections"/>
  <DeepDeleteHistory v-if="tab==='deep-delete'" :api="api"/>
  <template v-else>
  <p v-if="offlineStatusError&&['all','progress','failed'].includes(tab)" class="hint">{{offlineStatusError}}</p>
  <template v-if="tab==='progress'||(tab==='all'&&shownProgressCount)">
  <h2>进行中 <span class="task-count">{{shownProgressCount}}</span></h2>
  <div v-if="offlinePending.total" class="job-card">
    <strong>离线下载 <span class="task-count">{{offlinePending.total}}</span></strong>
    <p v-for="item in offlinePending.items" :key="item.id" class="hint">
      #{{item.id}} · {{fmtAgo(item.submitted_at)}} · {{item.target_label||'离线目录'}} · {{item.message||'等待 115 返回状态'}}
    </p>
    <p v-if="offlinePending.total>offlinePending.items.length" class="hint">显示最近 {{offlinePending.items.length}} 条</p>
  </div>
  <el-empty v-if="!shownProgressCount&&!offlineStatusError" description="当前没有进行中的任务" :image-size="64"/>
  <div v-for="(job,i) in shownActive" :key="job.id" v-memo="[job,busy]" class="job-card">
    <p class="hint"><span :title="new Date(job.updated*1000).toLocaleString()">{{fmtAgo(job.updated)}}</span> · #{{job.id}} · {{kinds[job.kind]||job.kind}} · {{taskSource(job)}}任务
      <el-tag :type="taskStatus(job).type" size="small">{{taskStatus(job).label}}</el-tag></p>
    <h3>{{taskPresentation(job).title}}</h3>
    <p v-if="job.state==='queued'" class="hint">等待同类任务或调度执行；入队不等于开始处理。</p>
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
  </template>
  <div v-if="tab==='failed'&&offlineReview.total" class="job-card">
    <strong>离线下载需核查 <span class="task-count">{{offlineReview.total}}</span></strong>
    <p v-for="item in offlineReview.items" :key="item.id" class="hint">
      #{{item.id}} · {{fmtAgo(item.submitted_at)}} · {{item.target_label||'离线目录'}} · {{item.message||'请到 115 核查'}}
    </p>
    <p v-if="offlineReview.total>offlineReview.items.length" class="hint">显示最近 {{offlineReview.items.length}} 条</p>
  </div>
  <template v-if="tab!=='progress'">
  <h2 v-if="shownHistory.length||tab!=='failed'||!offlineReview.total">已结束 <span class="task-count">{{shownHistory.length}}</span></h2>
  <el-empty v-if="!shownHistory.length&&!(tab==='failed'&&offlineReview.total)" description="这个分类下还没有已结束的任务" :image-size="64"/>
  <div v-for="job in shownHistory" :key="job.id" v-memo="[job,busy]" class="job-card">
    <p class="hint"><span :title="new Date(job.updated*1000).toLocaleString()">{{fmtAgo(job.updated)}}</span> · #{{job.id}} · {{kinds[job.kind]||job.kind}} · {{taskSource(job)}}任务
      <el-tag :type="taskStatus(job).type" size="small">{{taskStatus(job).label}}</el-tag></p>
    <h3>{{taskPresentation(job).title}}</h3>
    <p v-if="job.state==='needs_review'&&job.message" class="wrap">失败原因：{{job.message}}</p>
    <p v-if="taskPresentation(job).summary" class="wrap">{{taskPresentation(job).summary}}</p>
    <p v-for="note in taskPresentation(job).notes" :key="note" class="wrap">{{note}}</p>
    <div class="button-row">
      <el-button v-if="job.total||job.sync_metrics||job.state!=='completed'" :disabled="busy" @click="show(job.id)">查看处理结果</el-button>
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
  </template>
</section>
<el-drawer :model-value="!!detail" :title="detail?`任务 #${detail.job.id} 处理结果`:'处理结果'" size="min(900px, 100%)" @close="detail=null">
  <template v-if="detail">
    <h3>{{taskPresentation(detail.job).title}}</h3>
    <p class="wrap">当前阶段：{{detail.job.stage}}</p>
    <p v-if="detail.job.emby_target_name" class="wrap">Emby 入库通知 · {{detail.job.emby_target_name}}</p>
    <p v-if="taskPresentation(detail.job).summary" class="wrap">{{taskPresentation(detail.job).summary}}</p>
    <p v-for="note in taskPresentation(detail.job).notes" :key="note" class="wrap">{{note}}</p>
    <section v-if="syncMetrics" class="sync-timing">
      <h4>同步耗时</h4>
      <p>{{syncMetrics.mode==='full'?'全量同步':'增量同步'}}<template v-if="detail.job.sync_trigger==='transfer'"> · 转存后补扫目标目录</template></p>
      <p v-if="syncMetrics.scan">
        {{syncListings[syncMetrics.scan.listing] || '目录读取'}} · {{syncMetrics.scan.directories}}
        个{{syncMetrics.scan.directories_exact ? '目录' : '涉及目录'}} · {{syncMetrics.scan.files}} 个条目
      </p>
      <p>核对 {{syncMetrics.videos}} 个 · 复用 {{syncMetrics.reused}} 个 · STRM 新增 {{syncMetrics.created}} 个 / 更新 {{syncMetrics.updated}} 个</p>
      <p v-if="syncMetrics.requests">115 读取 {{syncMetrics.requests.read}} 次 · 限流 {{syncMetrics.requests.blocked}} 次</p>
      <dl>
        <div><dt>{{syncMetrics.attempt>1?'首次排队':'排队'}}</dt><dd>{{seconds(syncMetrics.queue)}}</dd></div>
        <div><dt>{{syncMetrics.attempt>1?'本次执行':'执行'}}</dt><dd>{{seconds(syncMetrics.execution)}}</dd></div>
        <div v-if="syncMetrics.attempt>1"><dt>任务总耗时</dt><dd>{{seconds(syncMetrics.total)}}</dd></div>
        <div v-for="stage in syncStages" :key="stage.key"><dt>{{stage.label}}</dt><dd>{{seconds(stage.value)}}</dd></div>
      </dl>
    </section>
    <details v-if="detail.job.message" class="task-extra"><summary>技术详情</summary><p class="wrap">{{detail.job.message}}</p></details>
    <div class="button-row">
      <el-radio-group v-model="itemFilter" size="small" aria-label="处理结果筛选">
        <el-radio-button value="all">全部 {{detail.items.length}}</el-radio-button>
        <el-radio-button value="unidentified">未识别 {{unidentifiedItems.length}}</el-radio-button>
        <el-radio-button value="failed">失败 {{failedItems.length}}</el-radio-button>
        <el-radio-button value="done">已处理 {{doneItems.length}}</el-radio-button>
      </el-radio-group>
      <el-button v-if="unidentifiedItems.length&&canHandleUnidentified" type="warning" plain :disabled="busy" @click="dismissUnrecognized()">忽略未识别并结项</el-button>
    </div>
    <p v-if="unidentifiedItems.length" class="hint">忽略只把未识别项标记为已跳过，不删除文件；文件仍在原目录或未识别目录。需要重新识别时先复制文件名到「识别测试」核对。</p>
    <el-empty v-if="!detailItems.length" description="这个筛选下没有条目" :image-size="56"/>
    <div v-for="item in detailItems" :key="item.id" class="job-card">
      <h3 class="wrap">{{item.original_name}}</h3><el-tag>{{item.step}}</el-tag>
      <p v-if="item.target_name" class="wrap">{{item.target_name}}</p>
      <p class="hint wrap">{{item.relative_path}}</p>
      <p v-if="item.message" class="wrap">{{item.message}}</p>
      <div v-if="isUnidentified(item)" class="button-row">
        <el-button v-if="canManualMatch" size="small" type="primary" plain :disabled="busy" @click="manualItem=item">手动匹配</el-button>
        <el-button size="small" @click="copyName(item.original_name)">复制文件名</el-button>
        <el-button v-if="canHandleUnidentified" size="small" type="warning" plain :disabled="busy" @click="dismissUnrecognized([item.id])">忽略此项</el-button>
      </div>
      <details class="task-extra"><summary>文件与洗版详情</summary>
        <p>文件 ID {{item.file_id}}</p>
        <p v-if="item.cloud_parts?.length">115 归档：{{item.cloud_parts.join(' / ')}}</p>
        <p v-if="item.tmdb_id">TMDB {{item.tmdb_id}} · {{item.media?.width}} × {{item.media?.height}} · {{item.media?.codec}}</p>
        <p v-if="item.recognition_alias" class="wrap">命中识别纠正 · {{item.recognition_alias.alias}} → TMDB {{item.recognition_alias.tmdb_id}}</p>
        <p v-if="item.upgrade?.decision">洗版决定：{{({replace:'更优替换',discard:'回收新副本',archive:'归档或共存',skip:'原位置跳过'} as Record<string,string>)[item.upgrade.decision]||item.upgrade.decision}}</p>
        <div v-for="check in item.upgrade?.comparisons||[]" :key="check.file_id"><p>比较旧文件 {{check.file_id}}：{{check.result==='identical'?'内容相同':check.result==='coexist'?'分组共存':check.result>0?'新版更优':check.result<0?'旧版更优':'同分'}}</p><p v-for="rule in check.rules" :key="rule.rule">{{rule.label}}：新 {{JSON.stringify(rule.new)}} / 旧 {{JSON.stringify(rule.old)}}</p></div>
        <p v-for="action in item.recycle||[]" :key="action.file_id">回收文件 {{action.file_id}}：{{action.state==='confirmed'?'已核实':'意图已保存，待核实'}}</p>
      </details>
    </div>
    <el-button v-if="detail.has_more" :disabled="busy" @click="show(detail.job.id,detail.items.at(-1).id)">后续文件</el-button>
  </template>
</el-drawer>
<ManualMatch v-if="manualItem&&detail" :api="api" :job-id="detail.job.id" :item="manualItem" @close="manualItem=null" @created="manualCreated"/>
</template>

<style scoped>
.sync-timing{margin:14px 0;padding:14px;border:1px solid var(--line);border-radius:10px;background:var(--surface-soft)}
.sync-timing h4{margin:0 0 8px;font-size:14px}
.sync-timing p{margin:5px 0;color:var(--muted);font-size:12px}
.sync-timing dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,130px),1fr));gap:8px;margin:12px 0 0}
.sync-timing dl>div{display:flex;flex-direction:column;gap:4px;padding:7px 8px;border-radius:5px;background:var(--surface)}
.sync-timing dt{font-size:12px;color:var(--muted)}
.sync-timing dd{margin:0;font-weight:600}
.tasks-panel > h2:first-of-type { margin-top: 10px; padding-top: 0; border-top: 0; }
@media (max-width: 600px) {
  .task-sections { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .task-sections :deep(button) { padding-inline: 2px; font-size: 12px; }
}

.tasks-panel>.hint{margin:0 0 12px}.tasks-panel>.job-card{border-left:3px solid color-mix(in srgb,var(--accent) 38%,var(--line))!important}.tasks-panel>.job-card h3{font-size:14px}.tasks-panel>.job-card .hint{line-height:1.6}.tasks-panel :deep(.el-empty){padding:20px 0}.tasks-panel :deep(.el-progress-bar__outer){background:var(--surface-soft)}.task-count{display:inline-block;min-width:20px;margin-left:5px;padding:2px 6px;border-radius:10px;background:var(--surface-soft);color:var(--muted);font-size:11px;text-align:center;vertical-align:middle}
@media(max-width:760px){.tasks-panel>.job-card{padding:13px}.tasks-panel :deep(.el-empty){padding:16px 0}}
</style>
