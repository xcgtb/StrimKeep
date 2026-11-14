<script setup lang="ts">
import {computed,ref,onMounted,onUnmounted} from 'vue'
import {useVisiblePolling} from './pageActivity'
import {ElMessage,ElMessageBox} from 'element-plus'
import {Setting} from '@element-plus/icons-vue'
import ShareProfile from './ShareProfile.vue'
const emit=defineEmits<{navigate:[page:string]}>()
const profileEditor=ref<any>(null)
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const shareText=ref(''),parsed=ref<any>(null),selected=ref<Set<string>>(new Set())
const busy=ref(false),error=ref('')
const settings=ref({auto_organize:true,share_check_hours:0,share_check_mode:'interval',share_check_cron:'0 3 * * *',share_prune_cron:'0 5 * * *'})
const settingsBaseline=ref('')
const monitorDirty=computed(()=>!!settingsBaseline.value&&JSON.stringify(settings.value)!==settingsBaseline.value)
// U07：状态由登记表驱动（分页完整返回），不再从路径文字推断；整理进入持久化任务。
const tab=ref<'pending'|'library'|'revoked'>('pending')
const rows=ref<any[]>([]),total=ref(0),offset=ref(0),limit=ref(50),query=ref('')
let refreshSequence=0
const watchJob=ref<number|null>(null),watchTimer=ref<number|null>(null)

async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}

async function loadSettings(updateOwn=true){try{const r=await props.api('/share/settings');if(updateOwn){settings.value={auto_organize:r.auto_organize,share_check_hours:r.share_check_hours??0,share_check_mode:r.share_check_mode??'interval',share_check_cron:r.share_check_cron??'0 3 * * *',share_prune_cron:r.share_prune_cron??'0 5 * * *'};settingsBaseline.value=JSON.stringify(settings.value)}}catch(e){if(updateOwn)error.value='分享监控设置读取失败：'+(e as Error).message}}
async function saveSettings(){await run(async()=>{await props.api('/share/settings','POST',settings.value);settingsBaseline.value=JSON.stringify(settings.value);ElMessage.success('监控设置已保存')})}
const checkMessage=ref('')
async function checkShares(){
  try{await ElMessageBox.confirm('将检查已登记的分享 STRM；确认失效且归属可核实的条目会撤销，并清理对应的本地 STRM 及附属文件。网络错误或状态不明确的条目会保留。','立即检查分享存活',{type:'warning',confirmButtonText:'开始检查',cancelButtonText:'取消'})}catch{return}
  await run(async()=>{const result=await props.api('/share/check','POST');checkMessage.value=result?.pending?(result.message||'检查正在后台进行，请查看实时日志。'):result?`检查完成：正常 ${result.alive??0}，失效已清理 ${result.dead_count??0}，无法判定 ${result.skipped??0}。`:'检查结果未确认，请查看实时日志。';await doRefresh()})
}

async function parse(){await run(async()=>{const r=await props.api('/share/parse','POST',{text:shareText.value});parsed.value=r;selected.value=new Set(r.videos.map((v:any)=>v.id));ElMessage.success(`解析到 ${r.video_count} 个视频`)})}
function toggle(id:string){const s=new Set(selected.value);s.has(id)?s.delete(id):s.add(id);selected.value=s}
function toggleAll(){if(selected.value.size===parsed.value?.videos.length)selected.value=new Set();else selected.value=new Set(parsed.value?.videos.map((v:any)=>v.id)||[])}

async function generate(){
  if(!parsed.value||!selected.value.size)return
  await run(async()=>{
    const files=parsed.value.videos.filter((v:any)=>selected.value.has(v.id))
    const r=await props.api('/share/generate','POST',{
      share_code:parsed.value.share_code,receive_code:parsed.value.receive_code,files})
    ElMessage.success(`已生成 ${r.count} 个 STRM 到待整理${r.skip_count?`，跳过 ${r.skip_count} 个已存在`:''}`)
    parsed.value=null;shareText.value='';tab.value='pending';await doRefresh(0)
  })
}
async function organize(){
  await run(async()=>{
    const r=await props.api('/share/organize','POST')
    ElMessage.success(`整理任务 #${r.job_id} 已入队，可在任务中心查看进度`)
    watchJob.value=r.job_id
    startWatching()
  })
}async function pruneOrphans(){
  try{await ElMessageBox.confirm('把文件已经不在的记录标成撤销，不影响影视库里还能播的条目。','清理孤儿记录',{confirmButtonText:'清理',cancelButtonText:'取消'})}catch{return}
  await run(async()=>{const r:any=await props.api('/share/strms/prune','POST');ElMessage.success(r.message||'已清理');await doRefresh(0)})
}

function startWatching(){
  if(watchTimer.value)window.clearInterval(watchTimer.value)
  watchTimer.value=window.setInterval(watch,3000)
}
async function watch(){
  if(!watchJob.value)return
  try{
    const r=await props.api('/organize/jobs/'+watchJob.value)
    const job=r.job||r
    if(['completed','partial','needs_review','cancelled','waiting_login'].includes(job.state)){
      stopWatching();watchJob.value=null
      ElMessage({message:'分享整理任务已结束：'+(job.message||job.stage||job.state),type:job.state==='completed'?'success':'warning'})
      await doRefresh(0)
    }
  }catch{stopWatching();watchJob.value=null}
}
function stopWatching(){if(watchTimer.value){window.clearInterval(watchTimer.value);watchTimer.value=null}}
async function revoke(id:string){
  try{await ElMessageBox.confirm('撤销后 STRM 文件会被删除，播放链接失效。','确认撤销',{confirmButtonText:'撤销',cancelButtonText:'取消'})}catch{return}
  await run(async()=>{await props.api('/share/strm/'+id,'DELETE');ElMessage.success('已撤销');await doRefresh()})
}
async function doRefresh(at?:number){
  const sequence=++refreshSequence,target=at??offset.value
  const r=await props.api(`/share/strms?state=${tab.value}&offset=${target}&limit=${limit.value}&q=${encodeURIComponent(query.value)}`)
  if(sequence!==refreshSequence)return
  offset.value=target;rows.value=r.items||[];total.value=r.total||0
}
async function refresh(){await run(()=>doRefresh())}
function switchTab(t:'pending'|'library'|'revoked'){if(busy.value)return;tab.value=t;void run(()=>doRefresh(0))}
function refreshAt(at:number){void run(()=>doRefresh(at))}
function fmtTime(ts:number){if(!ts)return '';return new Date(ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}

const tabLabels={pending:'待整理',library:'分享影视库',revoked:'已撤销'} as const
onMounted(async()=>{await refresh();await loadSettings()})
useVisiblePolling(async()=>{if(!busy.value)await loadSettings(false)},5000)
onUnmounted(stopWatching)
async function canLeave(){if(profileEditor.value&&!await profileEditor.value.canLeave())return false;if(!monitorDirty.value)return true;try{await ElMessageBox.confirm('监控设置还没有保存。','放弃修改？',{confirmButtonText:'放弃修改',cancelButtonText:'继续编辑'});settings.value=JSON.parse(settingsBaseline.value);return true}catch{return false}}
defineExpose({canLeave})
</script>
<template>
<div class="page-heading"><div><h1>分享 STRM</h1><p class="muted">从 115 分享链接直接生成 STRM；整理规则与分享监控在页面底部，默认收起。</p></div></div>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>
<el-alert v-if="watchJob" :title="`整理任务 #${watchJob} 进行中，完成后自动刷新列表`" type="info" :closable="false" class="global-error"/>


<section class="panel share-parse" v-if="!parsed"><h2>解析分享链接</h2>
<el-form label-position="top"><el-form-item label="分享链接（可含提取码）">
<el-input v-model="shareText" :disabled="busy" placeholder="https://115.com/s/xxxxx?password=yyyy" @keydown.enter="parse"/></el-form-item></el-form>
<el-button type="primary" :disabled="busy||!shareText.trim()" @click="parse" style="margin-top:8px">解析链接</el-button>
</section>

<section class="panel" v-if="parsed">
<div class="section-title"><h2>分享内容（{{parsed.video_count}} 个视频）</h2><el-button text @click="parsed=null">← 重新输入</el-button></div>
<div class="filter-row" style="margin-bottom:12px">
<el-button size="small" @click="toggleAll">{{selected.size===parsed.videos.length?'全不选':'全选'}}</el-button>
<el-button type="primary" size="small" :disabled="busy||!selected.size" @click="generate">生成 {{selected.size}} 个 STRM</el-button>
</div>
<div class="share-list">
<div v-for="v in parsed.videos" :key="v.id" class="share-card pick" :class="{selected:selected.has(v.id)}" @click="toggle(v.id)">
<el-checkbox :model-value="selected.has(v.id)" :aria-label="'选择 '+v.name" @change="toggle(v.id)" @click.stop/>
<div class="pick-text"><span class="share-name">{{v.name}}</span><span class="share-path" v-if="v.path">{{v.path}}</span></div>
</div>
</div>
</section>

<section class="panel" >
<div class="library-heading"><div><h2>分享内容</h2><p>管理生成的 STRM</p></div><span class="library-count">{{total}} 项</span></div>
<div class="section-title share-actions">
<button v-for="(label,t) in tabLabels" :key="t" class="library-tab" :class="{active:tab===t}" type="button" @click="switchTab(t)">{{label}}</button>
<el-input v-model="query" size="small" placeholder="搜索名称/分享码" style="max-width:200px" clearable @change="refreshAt(0)"/>
<el-button v-if="tab==='pending'" type="primary" size="small" :disabled="busy||!rows.length" @click="organize">整理 → 后台任务</el-button><el-button size="small" :disabled="busy" @click="pruneOrphans" title="文件已不存在的记录标撤销">清理孤儿记录</el-button>
</div>
<el-empty v-if="!rows.length" description="没有条目" :image-size="65"/>
<div class="share-list library-list">
<div v-for="s in rows" :key="s.id" class="share-card" :class="{revoked:s.revoked}">
<div class="share-card-main">
<span class="share-name">{{s.name}}</span>
<el-tag v-if="s.revoked" size="small" type="info">已撤销</el-tag>
<span class="share-time">{{fmtTime(s.created)}}</span>
<el-button v-if="!s.revoked" text type="danger" size="small" @click="revoke(s.id)">撤销</el-button>
</div>
<p class="share-path" :title="s.strm_path">{{s.strm_path}}</p>
</div>
</div>
<div style="display:flex;justify-content:flex-end;margin-top:10px" v-if="total>limit">
<el-pagination layout="prev, pager, next" :total="total" :page-size="limit" :current-page="offset/limit+1"
  @current-change="(p:number)=>refreshAt((p-1)*limit)"/>
</div>
</section>
<section class="panel">
<ShareProfile ref="profileEditor" :api="api"/>
<div class="monitor-section"><div class="monitor-heading"><div><h2>分享监控</h2><p>控制自动整理和存活检测</p></div><el-button :disabled="busy||!monitorDirty" @click="saveSettings">保存</el-button></div>
<div class="monitor-row"><div><strong>自动整理</strong><p>监控发现新文件后，加入整理任务。</p></div><el-switch v-model="settings.auto_organize"/></div>
<div class="monitor-row"><div><strong>台账核对</strong><p>按 cron 清理「文件已不存在」的分享 STRM 记录，默认每天凌晨 5 点。</p></div><div class="monitor-input"><el-input v-model="settings.share_prune_cron" size="small" style="width:132px" placeholder="0 5 * * *"/><span>分 时 日 月 周</span></div></div><div class="monitor-row"><div><strong>存活检测</strong><p>{{settings.share_check_mode==='cron'?'按 Cron 表达式检查分享，只在明确失效时撤销。':(settings.share_check_hours>0?`每 ${settings.share_check_hours} 小时检查一次分享。`:'填 0 则关闭存活检测。')}}</p></div><div class="monitor-input"><el-radio-group v-model="settings.share_check_mode" size="small"><el-radio-button value="interval">间隔</el-radio-button><el-radio-button value="cron">Cron</el-radio-button></el-radio-group><template v-if="settings.share_check_mode==='interval'"><el-input-number v-model="settings.share_check_hours" :min="0" :max="8760" :step="1" size="small"/><span>小时</span></template><template v-else><el-input v-model="settings.share_check_cron" size="small" style="width:132px" placeholder="0 3 * * *"/><span>分 时 日 月 周</span></template></div></div></div>
<div class="button-row"><el-button :loading="busy" @click="checkShares">立即检查分享存活</el-button><span v-if="checkMessage" class="hint">{{checkMessage}}</span></div>
</section>

</template>
<style scoped>
.share-list{display:grid;gap:10px;min-width:0}
.share-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface-soft);min-width:0}
.share-card.pick{cursor:pointer;display:flex;align-items:center;gap:10px}
.share-card.revoked{opacity:.5}
.share-card-main{display:flex;align-items:center;gap:10px;min-width:0}
.share-name{flex:1;min-width:0;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.share-time{font-size:11px;color:var(--muted);flex-shrink:0}
.share-path{margin:6px 0 0;font-size:11px;line-height:1.6;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pick-text{display:grid;gap:2px;min-width:0}
.settings-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.setting-item{border:1px solid var(--el-border-color-lighter,#e4e7ed);border-radius:8px;padding:14px}
.setting-item .hint{font-size:11px;line-height:1.6;margin:6px 0 0;color:var(--el-text-color-secondary)}
.section-title{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
</style>

<style scoped>.inherited-settings p{overflow-wrap:anywhere;line-height:1.7;font-size:13px}.inherited-settings{min-width:0}</style>

<style scoped>

.share-card{background:var(--surface);border-radius:9px;padding:14px 15px;transition:border-color .15s,background .15s}.share-card:hover{border-color:color-mix(in srgb,var(--accent) 45%,var(--line));background:var(--surface-soft)}.share-card.pick{min-height:52px}.share-name{font-weight:600;color:var(--text)}.share-path{font-family:ui-monospace,monospace;overflow-wrap:anywhere}.share-time{font-variant-numeric:tabular-nums}.inherited-settings{padding:5px 14px 14px;border:1px solid var(--line);border-radius:9px;background:var(--surface-soft)}.setting-item{margin-top:14px;border-color:var(--line)}.share-actions{justify-content:flex-start}.share-actions :deep(.el-input){margin-left:auto}.share-parse :deep(.el-input__wrapper){min-height:42px}
.share-card.pick.selected{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 7%,var(--surface))}.share-card.pick:focus-within{outline:2px solid var(--accent);outline-offset:2px}
.library-heading,.monitor-heading,.monitor-row{display:flex;align-items:center;justify-content:space-between;gap:14px}.library-heading h2,.monitor-heading h2{margin:0;font-size:16px}.library-heading p,.monitor-heading p,.monitor-row p{margin:5px 0 0;font-size:12px;color:var(--muted);line-height:1.5}.library-count{font-size:12px;color:var(--muted)}.share-actions{padding:18px 0 12px;border-bottom:1px solid var(--line)}.library-tab{border:0;border-bottom:2px solid transparent;background:transparent;color:var(--muted);padding:8px 2px;font-size:13px;cursor:pointer}.library-tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}.library-tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.library-list{gap:0}.share-card:not(.pick){border:0;border-bottom:1px solid var(--line);border-radius:0;padding:13px 0;background:transparent}.share-card:not(.pick):hover{background:transparent}.share-card:not(.pick):last-child{border-bottom:0}.monitor-section{margin-top:24px;padding-top:20px;border-top:1px solid var(--line)}.monitor-row{padding:14px 0;border-bottom:1px solid var(--line)}.monitor-row:last-child{border-bottom:0}.monitor-row strong{font-size:13px}.monitor-input{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px}
@media(max-width:760px){.share-card{padding:12px}.share-card-main{flex-wrap:wrap}.share-name{min-width:50%;white-space:normal;overflow-wrap:anywhere}.share-path{white-space:normal}.share-actions{gap:7px}.share-actions :deep(.el-input){flex:1 0 100%;max-width:none!important;margin-left:0}.share-actions :deep(.el-button){margin:0}}
</style>
