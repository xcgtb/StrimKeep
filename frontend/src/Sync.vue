<script setup lang="ts">
import {useVisiblePolling} from './pageActivity'
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
import {useRoute} from 'vue-router'
import {ElMessage,ElMessageBox} from 'element-plus'
import {VideoPlay,FolderOpened,Connection,Bell,DocumentChecked} from '@element-plus/icons-vue'
import DirectoryPicker from './DirectoryPicker.vue'
import SettingCard from './SettingCard.vue'
import ExtensionEditor from './ExtensionEditor.vue'
import {VIDEO_PRESETS,METADATA_PRESETS} from './extensions'
import WorkflowToolbar from './WorkflowToolbar.vue'
import AutomationSettings from './AutomationSettings.vue'
import {taskPresentation} from './taskPresentation'
import {copyText} from './clipboard'
import {scheduleDraft,schedulePatch,scheduleIdentity,syncBaselineWithWebhookToken,type ScheduleDraft} from './workflowSettings'
const route=useRoute()
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const outputRoot=ref(''),directCid=ref(''),savedBase=ref('')
const config=ref<any>({inherit_video_extensions:true,video_extensions:[],metadata_extensions:[],min_video_size_mb:0,generate_nfo:false,generate_images:false,emby_media_info:false,stale_detect:false,webhook_token:'',deep_delete_dry_run:true,cleanup_empty:true,source_cids:[] as string[],directory_labels:{} as Record<string,string>,notify_emby:true,full_scan:true,cleanup_missing:true}),base=ref('')
const schedule=ref<ScheduleDraft>(scheduleDraft({},'sync')),baseline=ref(''),scheduleBaseline=ref(''),ready=ref(false),configured=ref(false)
const picker=ref(''),busy=ref(false),error=ref(''),query=ref(''),nextDue=ref<number>(),latest=ref<any>(null)
const audit=ref<any>(null)

const snapshot=()=>JSON.stringify({config:config.value,base:base.value})
const dirty=computed(()=>ready.value&&(snapshot()!==baseline.value||scheduleIdentity(schedule.value,'sync')!==scheduleBaseline.value))
const label=(id:string,scheduled=false)=>(scheduled?schedule.value.directory_labels:config.value.directory_labels)[id]||id
async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function select(folder:{id:string,name:string}){if(picker.value==='scheduled'){if(!schedule.value.source_cids.includes(folder.id))schedule.value.source_cids.push(folder.id);schedule.value.directory_labels[folder.id]=folder.name}else{if(!config.value.source_cids.includes(folder.id))config.value.source_cids.push(folder.id);config.value.directory_labels[folder.id]=folder.name}picker.value=''}
function addCid(){const cid=directCid.value.trim();if(!/^[1-9]\d{0,23}$/.test(cid)){ElMessage.warning('请输入非根目录 CID');return}if(!config.value.source_cids.includes(cid))config.value.source_cids.push(cid);directCid.value=''}
function sameDirectories(){schedule.value.source_cids=[...config.value.source_cids];schedule.value.directory_labels={...schedule.value.directory_labels,...config.value.directory_labels}}
async function refreshJob(){latest.value=(await props.api('/organize/jobs')).items.find((j:any)=>j.kind==='strm_sync')||null}
const webhookUrl=ref('')
watch([()=>config.value.webhook_token,()=>base.value],([token,base])=>{webhookUrl.value=token&&base?base.replace(/\/+$/,'')+'/api/v1/emby/webhook?token='+token:''},{immediate:true})
async function genToken(){await run(async()=>{
  // 令牌由服务端 CSPRNG 生成：它能删网盘文件，不能由浏览器拼出来。
  const r=await props.api('/sync/webhook-token','POST',{})
  config.value.webhook_token=r.webhook_token
  baseline.value=syncBaselineWithWebhookToken(baseline.value,r.webhook_token)
  ElMessage.success('深度删除令牌已由服务端生成并保存')
})}
async function copyUrl(){try{await copyText(webhookUrl.value,navigator.clipboard,document);ElMessage.success('完整地址已复制')}catch(e){ElMessage.error((e as Error).message||'复制失败，请手动复制完整地址')}}
// 链路自检：只读、不依赖 Emby 是否已配置、永远不删任何东西。
async function selftest(){await run(async()=>{
const r:any=await props.api('/emby/webhook/selftest','POST',{})
const rows:string[]=[`<b>Emby 挂载前缀</b>：${r.emby_prefix}　<b>预演</b>：${r.dry_run?'开（不会真删）':'关（会真删）'}`]
for(const c of r.cases||[])rows.push(`${c.case}：命中 <b>${c.managed+c.share}</b> 条`)
rows.push(`<b>通用目录名反例</b>：命中 ${r.generic_directory_matched} 条（正常应为 0）`)
rows.push(r.last_received?`最近一次事件：${r.last_received}`:'最近还没有收到过真实的 Emby 事件')
await ElMessageBox.alert(rows.join('<br/>'),'深度删除链路自检',{dangerouslyUseHTMLString:true,confirmButtonText:'关闭'})
})}

const rewriteMsg=ref('')
const cleanupMsg=ref('')
async function checkStrm(){await run(async()=>{audit.value=await props.api('/strm/audit?limit=20')})}
async function cleanStrm(){
  try{await ElMessageBox.confirm('将删除「非本项目」的 STRM（内容不是 /play115/ 格式的），并撤销台账里文件已经不在的登记。本项目的 STRM 一个都不删。','清理 STRM 残次品',{confirmButtonText:'开始清理',cancelButtonText:'取消'})}catch{return}
  await run(async()=>{const r:any=await props.api('/strm/audit/cleanup','POST',{});ElMessage.success(`已删除 ${r.removed} 个非本项目 STRM，撤销 ${r.revoked_playbacks+r.revoked_shares} 条悬空登记`);audit.value=await props.api('/strm/audit?limit=20')})
}

async function cleanupPlaybacks(){await run(async()=>{const r:any=await props.api('/playbacks/cleanup','POST',{});cleanupMsg.value=r.message||`已清理 ${r.removed} 条`;ElMessage.success(cleanupMsg.value)})}
async function rewrite(){await run(async()=>{const p:any=await props.api('/strm/rebuild?dry_run=1','POST',{});if(!p.rewritten&&!p.missing){rewriteMsg.value=`无需重写：${p.unchanged} 条已是最新`;ElMessage.success('本地 STRM 与当前播放地址一致');return}try{await ElMessageBox.confirm(`将重写 ${p.rewritten} 条，缺失 ${p.missing} 条（缺失只报告、不创建）。执行前会自动快照，可一键回滚。`,'按当前地址重写本地 STRM',{confirmButtonText:'开始重写',cancelButtonText:'取消'})}catch{return}const r:any=await props.api('/strm/rebuild','POST',{});rewriteMsg.value=`重写 ${r.rewritten} · 未变 ${r.unchanged} · 缺失 ${r.missing} · 失败 ${r.failed}`;ElMessage.success(r.snapshot?`已重写 ${r.rewritten} 条，快照 #${r.snapshot} 可回滚`:'本地 STRM 已按当前播放地址重写')})}
async function rollbackRewrite(){await run(async()=>{const s:any=await props.api('/strm/rebuild/status');if(!s||!s.snapshot){ElMessage.warning('还没有可回滚的重写记录');return}try{await ElMessageBox.confirm(`把上次重写（快照 #${s.snapshot}）写回的本地 STRM 内容还原？不影响云端文件与播放身份。`,'回滚上次重写',{confirmButtonText:'回滚',cancelButtonText:'取消'})}catch{return}const r:any=await props.api('/strm/rebuild/rollback','POST',{});rewriteMsg.value=`已回滚 ${r.restored}/${r.total} 条（快照 #${r.snapshot}）`;ElMessage.success('已回滚上次重写')})}
async function save(){await run(async()=>{await props.api('/sync/config','PUT',config.value);let saved='同步目录';try{if(base.value!==savedBase.value){await props.api('/settings','PUT',{playback_base:base.value});savedBase.value=base.value;saved+='、播放地址'};if(scheduleIdentity(schedule.value,'sync')!==scheduleBaseline.value){const r=await props.api('/system/schedules','PUT',schedulePatch('sync',schedule.value));schedule.value=scheduleDraft(r.config,'sync');nextDue.value=r.states.find((s:any)=>s.kind==='strm_sync')?.next_due}}catch(e){throw new Error(saved+'已保存；其余修改未全部保存：'+(e as Error).message)}baseline.value=snapshot();scheduleBaseline.value=scheduleIdentity(schedule.value,'sync');configured.value=!!config.value.source_cids.length&&!!base.value;ElMessage.success('STRM 同步设置已保存')})}
const syncState=ref<any>(null)
async function loadSyncState(){try{syncState.value=await props.api('/sync/state')}catch{syncState.value=null}}
function watermarkText(){
  const at=Number(syncState.value?.watermark?.at||0)
  if(!at)return ''
  const d=new Date(at*1000),pad=(n:number)=>String(n).padStart(2,'0')
  const mode=syncState.value?.watermark?.mode==='incremental'?'增量':'全量'
  return `${d.getMonth()+1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}（${mode}）`
}
async function startIncremental(){if(dirty.value)return;await run(async()=>{const r:any=await props.api('/sync/incremental','POST',{});latest.value=r;ElMessage.success(`增量同步任务 #${r.id} 已加入队列（只处理上次成功同步之后的变化）`);await loadSyncState()})}
async function startFull(){
  if(dirty.value)return
  try{await ElMessageBox.confirm('全量重建会重新读取所选云盘目录并逐文件核对，比增量慢；换过库根或改过命名口径之后才需要。','全量重建 STRM',{confirmButtonText:'开始全量',cancelButtonText:'取消',type:'warning'})}catch{return}
  await run(async()=>{const r:any=await props.api('/sync/full','POST',{});latest.value=r;ElMessage.success(`全量重建任务 #${r.id} 已加入队列`);await loadSyncState()})
}
async function canLeave(){if(!dirty.value)return true;try{await ElMessageBox.confirm('当前修改尚未保存，修改会暂存到当前页面，刷新网页前请记得保存。确定切换？','离开同步设置',{confirmButtonText:'暂存并切换',cancelButtonText:'继续编辑'});return true}catch{return false}}
function beforeUnload(e:BeforeUnloadEvent){if(dirty.value&&route.name==='sync'){e.preventDefault();e.returnValue=''}}
defineExpose({canLeave})
onMounted(()=>{window.addEventListener('beforeunload',beforeUnload);run(async()=>{const [data,timers,settings]=await Promise.all([props.api('/sync/config'),props.api('/system/schedules'),props.api('/settings')]);config.value=data.config;outputRoot.value=data.output_root;base.value=settings.playback_base;savedBase.value=base.value;schedule.value=scheduleDraft(timers.config,'sync');nextDue.value=timers.states.find((s:any)=>s.kind==='strm_sync')?.next_due;baseline.value=snapshot();scheduleBaseline.value=scheduleIdentity(schedule.value,'sync');configured.value=!!config.value.source_cids.length&&!!base.value;ready.value=true;await loadSyncState();await refreshJob()})})
onUnmounted(()=>{window.removeEventListener('beforeunload',beforeUnload)})
useVisiblePolling(async()=>{if(!busy.value){await refreshJob();if(!dirty.value){const [data,settings]=await Promise.all([props.api('/sync/config'),props.api('/settings')]);config.value=data.config;base.value=settings.playback_base;savedBase.value=base.value;baseline.value=snapshot()}}},5000)
</script>
<template><div class="workflow-page">
<WorkflowToolbar v-model:query="query" title="STRM 生成" :dirty="dirty" :busy="busy" :ready="ready" @save="save"/>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>
<SettingCard title="同步执行与定时" description="立即同步一次，或按设定间隔自动同步。" :summary="schedule.enabled?`定时已开启 · ${schedule.mode==='cron'?schedule.cron:'每 '+schedule.minutes+' 分钟'}`:'定时已关闭 · 可手动同步'" :icon="VideoPlay" :query="query" keywords="立即 自动 定时 间隔 任务" default-open>
<div class="execution-row"><div><strong>同步 STRM 与绑定字幕</strong><p>把云盘里的视频对齐成 Emby 能播的 .strm（含字幕、NFO）。</p></div><el-button type="primary" :icon="VideoPlay" :disabled="busy||!ready||!configured||dirty" title="只处理上次同步之后新增、改名或换内容的文件；本地 .strm 丢了也会一起补回" @click="startIncremental">增量同步</el-button><el-button :disabled="busy||!ready||!configured||dirty" title="重新读取所选云盘目录并逐文件核对，整库重建；只在换过媒体库目录或命名规则后用" @click="startFull">全量重建</el-button></div><p v-if="dirty" class="inline-notice">请先保存修改，再开始同步。</p><p v-else-if="ready&&!configured" class="inline-notice">先选择同步目录和播放地址，再保存修改。</p><p class="hint">平时点「增量同步」就够：只补云盘里新出现、改名或换内容的文件，几秒到几十秒；本地 .strm 丢了也会一起补回来。换过媒体库目录、改过命名规则或换过 115 账号之后，点一次「全量重建」把整库重新核对（会慢一些）。上次：{{watermarkText()||'还没同步过'}}</p>
<AutomationSettings v-model="schedule" kind="sync" :busy="busy" :next-due="nextDue"/>
<div class="directory-group"><h3>定时同步目录 <small>单独保存</small></h3><div v-for="id in schedule.source_cids" :key="id" class="directory-item"><el-icon><FolderOpened/></el-icon><span>{{label(id,true)}}</span><el-button text :disabled="busy" @click="schedule.source_cids=schedule.source_cids.filter(x=>x!==id)">移除</el-button></div><div class="button-row"><el-button :disabled="busy" @click="picker='scheduled'">添加定时同步目录</el-button><el-button :disabled="busy||!config.source_cids.length" @click="sameDirectories">使用下方手动同步目录</el-button></div><p class="hint">点击“使用下方手动同步目录”只复制当前选择，之后可各自修改。</p></div>
<div v-if="latest" class="task-peek"><span>最近任务 #{{latest.id}}</span><strong>{{taskPresentation(latest).title}}</strong><el-button text type="primary" @click="emit('navigate','tasks')">查看任务进度 →</el-button></div>
</SettingCard>
<SettingCard title="手动同步来源目录" description="点击“立即同步 STRM”时，读取这些目录及子目录。" :summary="`${config.source_cids.length} 个手动同步目录`" :icon="FolderOpened" :query="query" keywords="115 来源 源目录 文件夹 范围" default-open>
<div v-for="id in config.source_cids" :key="id" class="directory-item"><el-icon><FolderOpened/></el-icon><span>{{label(id)}}</span><el-button text :disabled="busy" @click="config.source_cids=config.source_cids.filter((x:string)=>x!==id)">移除</el-button></div><el-button :disabled="busy" @click="picker='manual'">选择来源目录</el-button><div class="strm-field"><el-input v-model="directCid" placeholder="也可直接输入115目录 CID" :disabled="busy" @keyup.enter="addCid"/><el-button :disabled="busy" @click="addCid">添加 CID</el-button></div><p class="hint">来源 CID：{{config.source_cids.join('、')||'尚未选择'}}。生成位置：{{outputRoot||'读取中'}}。这是 StrimKeep 容器内路径，宿主机位置由 Docker 挂载决定。</p><p class="hint">已整理的归档保持媒体库结构；其他目录输出到“StrimKeep同步”。</p><div class="workflow-switch-row"><span>全量同步后自动清理失效的本地 STRM</span><el-switch v-model="config.cleanup_missing"/></div><p class="hint">开启后，仅在完整扫描并再次确认网盘源文件已删除或移出所选范围时，清理本项目登记且内容未被修改的本地输出；不会删除 115 网盘文件。关闭后只同步新增和变更，已有失效条目保留在本地。</p>
</SettingCard>
<SettingCard title="播放地址" description="播放器访问 StrimKeep 的基础地址，写入 STRM。" :summary="base||'尚未配置播放地址'" :icon="Connection" :query="query" keywords="URL 域名 端口 IP 公网 局域网 302" default-open><el-form label-position="top"><el-form-item label="StrimKeep 播放基础地址"><el-input v-model="base" :disabled="busy" placeholder="http://strimkeep.example.com:9000"/></el-form-item></el-form><div class="button-row" style="margin:10px 0"><el-button :disabled="busy||!base" @click="rewrite">按当前地址重写全部本地 STRM</el-button><span v-if="rewriteMsg" class="hint" style="margin:0">{{rewriteMsg}}</span><el-button :disabled="busy" @click="rollbackRewrite">回滚上次重写</el-button><el-button :disabled="busy" @click="cleanupPlaybacks">清理未被引用的播放身份</el-button><span v-if="cleanupMsg" class="hint" style="margin:0">{{cleanupMsg}}</span></div><p class="hint">改完地址保存后，点上面的按钮可以把所有 STRM 的入口批量换成新地址：只改本地文件与台账，不读云端、不动网盘。换 IP、加域名、内网切公网时用它，不必整库重扫。</p></SettingCard>
<SettingCard title="STRM 体检" description="对一遍本地 STRM：本项目管理多少、多少是别的工具写的、有没有登记了却没文件的。" :summary="audit?`共 ${audit.total} 个 · 非本项目 ${audit.external_total} · 台账悬空 ${audit.missing_total}`:'还没检查'" :icon="DocumentChecked" :query="query" keywords="STRM 体检 孤儿 台账 外部 重复 一致性" :default-open="false">
  <div class="button-row" style="margin:0 0 10px"><el-button :disabled="busy" @click="checkStrm">开始检查</el-button><el-button type="danger" plain :disabled="busy||!audit||(!audit.external_total&&!audit.missing_total)" @click="cleanStrm">清理残次品</el-button><span class="hint" style="margin:0">检查只读；清理只删「非本项目」的 STRM 与悬空登记</span></div>
  <template v-if="audit">
    <p class="hint" style="margin:0 0 8px">本地共 {{audit.total}} 个 STRM，其中本项目登记 {{audit.managed}} 个。</p>
    <div v-if="audit.external_total">
      <p style="margin:0 0 4px"><b>非本项目 STRM：{{audit.external_total}} 个</b>（另一条 STRM 产线或旧工具写的，不进我们的台账，但会和我们的文件一起被 Emby 收录，容易产生重复条目）</p>
      <p v-for="item in audit.external" :key="item.path" class="hint wrap" style="margin:0 0 2px">{{item.kind==='legacy'?'旧格式':'其它'}} · {{item.path}}</p>
      <p v-if="audit.external_total>audit.external.length" class="hint">…仅显示前 {{audit.external.length}} 条</p>
    </div>
    <div v-if="audit.missing_total" style="margin-top:10px">
      <p style="margin:0 0 4px"><b>台账有、文件不在：{{audit.missing_total}} 个</b>（登记还在但磁盘上没了；不影响播放，只让统计偏大）</p>
      <p v-for="path in audit.missing" :key="path" class="hint wrap" style="margin:0 0 2px">{{path}}</p>
    </div>
    <p v-if="!audit.external_total && !audit.missing_total" class="hint" style="margin:0">✓ 台账与磁盘一致，没有外部 STRM。</p>
  </template>
</SettingCard>
<SettingCard title="STRM 生成过滤与附属文件" description="与整理格式分开控制；分享 STRM 自动继承这里的生成规则。" :icon="DocumentChecked" :query="query" keywords="扩展名 过滤 视频 元数据 字幕 大小 阈值" default-open>
<div class="workflow-switch-row"><strong>视频扩展名沿用影视整理</strong><el-switch v-model="config.inherit_video_extensions" :disabled="busy"/></div>
<ExtensionEditor v-model="config.video_extensions" label="STRM 视频扩展名" :presets="VIDEO_PRESETS" :max-items="60" :excluded="config.metadata_extensions" :forbidden="['.strm']" :disabled="busy||config.inherit_video_extensions"/>
<p class="hint">开启沿用后，以影视整理保存的扩展名为准；此处显示的是独立设置，关闭沿用后可编辑。未选格式不生成新 STRM，保留原视频和已有输出。</p>
<div class="workflow-switch-row"><div><strong>生成 NFO</strong><p>在 STRM 旁边写电影/单集的 NFO，剧集还会写 tvshow.nfo；Emby 直接读本地元数据，不用再联网刮削。同步老库时也会现场识别补齐，认不出的文件跳过并记录原因。</p></div><el-switch v-model="config.generate_nfo" :disabled="busy"/></div>
<div class="workflow-switch-row"><div><strong>下载海报与背景图</strong><p>从 TMDB 取 poster.jpg / fanart.jpg 放进作品目录，失败只跳过，不影响 STRM。</p></div><el-switch v-model="config.generate_images" :disabled="busy"/></div>
<div class="workflow-switch-row"><div><strong>Emby 媒体信息</strong><p>需要启用 Emby 入库通知。网盘和分享 STRM 入库后，后台提取缺失的媒体信息，并在 STRM 旁保存 -media-info.json。无需安装插件；Emby 可能再次读取视频。失败不影响整理和 STRM。</p></div><el-switch v-model="config.emby_media_info" :disabled="busy" aria-label="Emby 媒体信息"/></div>
<div class="strm-field"><label>不生成 STRM 的小视频阈值（MB，0 不限制）</label><el-input-number v-model="config.min_video_size_mb" :min="0" :max="1048576" :disabled="busy"/></div>
<ExtensionEditor v-model="config.metadata_extensions" label="实体下载的字幕与元数据扩展名" :presets="METADATA_PRESETS" :max-items="60" :excluded="config.inherit_video_extensions?[]:config.video_extensions" :forbidden="['.strm']" :disabled="busy"/>
<p class="hint">视频只生成链接；这些扩展名实体下载。普通字幕绑定对应视频；其余附属文件增量下载到对应目录，不覆盖已有文件。单个附属文件上限20MB，超限说明并跳过。</p>
<div class="workflow-switch-row"><strong>清理失效输出后的空目录</strong><el-switch v-model="config.cleanup_empty" :disabled="busy"/></div>
</SettingCard>

<SettingCard title="深度删除（Emby Webhook 联动）" description="配置 Emby 删除事件的联动与预演。" :summary="config.webhook_token?'令牌已生成':'尚未生成令牌'" :icon="Connection" :query="query" keywords="Webhook 删除 令牌 预演 Emby" :default-open="false"><p class="hint" style="margin:0 0 12px">在 Emby 里删除电影/单集时自动删除网盘文件（进回收站可还原）和本地 strm。需要配置 <b>Emby 的 Webhook 插件</b>（不是第三方插件的通知），按以下步骤配置：</p><el-form label-position="top" :disabled="busy"><el-form-item label="第一步：点「生成令牌」，点完自动保存"><div class="channel-dir"><el-button type="primary" :disabled="busy||!ready" @click="genToken">{{config.webhook_token?'重新生成':'生成令牌'}}</el-button><el-input v-model="config.webhook_token" readonly style="max-width:260px" placeholder="点生成"/><span v-if="config.webhook_token" class="hint" style="margin:0">✓ 已自动保存</span></div></el-form-item><el-form-item label="第二步：复制下方 Webhook 地址"><div class="channel-dir"><el-input :model-value="webhookUrl" type="textarea" :rows="3" readonly aria-label="完整 Webhook 地址" placeholder="配置播放地址后自动出现"/><el-button :disabled="!webhookUrl" @click="copyUrl">一键复制</el-button></div><p class="hint" style="margin:4px 0 0">自动使用上方配置的播放地址（Emby 播放 strm 时就在用这个地址，必然可达）。内网填 <code>http://你的NAS地址:9000</code>，公网填域名——配好播放地址后这里自动跟上，无需手动改。</p></el-form-item><el-form-item label="第三步：在 Emby 里配置 Webhook 通知"><div style="font-size:13px;line-height:2;color:var(--text)"><p style="margin:0">Emby 控制台 → 通知 → 添加通知 → 选 <b>Webhook</b>：</p><p style="margin:2px 0 0">① URL 粘贴上面复制的地址<br/>② 请求方法：<b>POST</b><br/>③ 内容类型：<b>application/json</b>（不要选 multipart）<br/>④ 事件类型只勾：<b>Item Removed（项目已删除）</b><br/>⑤ 保存</p></div></el-form-item><el-form-item label="第四步：预演验证（先开着，确认无误后关闭）"><el-switch v-model="config.deep_delete_dry_run"/><p class="hint" style="margin:4px 0 0">预演只记录将删什么。去 Emby 删个测试文件，实时日志里看到命中正确后关掉再保存。</p></el-form-item><el-form-item label="第五步（可选）：链路自检"><div class="channel-dir"><el-button :disabled="busy" @click="selftest">自检</el-button><span class="hint" style="margin:0">不碰 Emby 也能验证：用台账里的真实路径跑一遍匹配，只读、永远不删</span></div></el-form-item></el-form><p class="hint">之后你在 Emby 里删除电影、单集、整季或整部剧集，Emby Webhook 就会发事件过来：网盘库 → 本地 strm + NFO 删除 + 网盘文件进 115 回收站（可还原）；分享库 → 只删本地 strm。整理或转存运行时后台队列会自动让路，等它们结束再继续删。</p></SettingCard>
<SettingCard title="通知 Emby 入库" description="本批输出有变化后通知 Emby，并检查入库结果。" :summary="config.notify_emby?'自动通知已开启':'自动通知已关闭'" :icon="Bell" :query="query" keywords="通知 Emby 扫描 入库" :default-open="false"><div class="workflow-switch-row"><div><strong>同步后通知 Emby</strong><p>只通知本批新增、改名和删除路径，无变化不发送；核对超时只刷新命中的目标库。</p></div><el-switch v-model="config.notify_emby" :disabled="busy" aria-label="同步后通知 Emby"/></div></SettingCard>
<SettingCard title="同步范围与失效清理" description="完整清单与云端状态核实后，维护本地受管输出。" summary="只清理所选输出范围内的 STRM，保留 NFO 和图片" :icon="DocumentChecked" :query="query" keywords="字幕 删除 清理 输出 Token 规则" :default-open="false"></SettingCard>
<p v-if="query" class="search-caption">仅显示匹配的设置卡片；清空搜索可查看全部。</p>
<DirectoryPicker v-if="picker" :api="api" @select="select" @close="picker=''"/>
</div></template>

<style scoped>.strm-field{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:12px 0;min-width:0}.strm-field label{width:100%;font-size:13px}.strm-field :deep(.el-select){width:100%;min-width:0}.strm-field>.el-input{flex:1;min-width:160px}</style>

<style scoped>

.workflow-page :deep(.directory-item){align-items:center}.workflow-page :deep(.directory-group h3){font-size:13px}.strm-field{padding:2px 0}.strm-field label{color:var(--text);font-weight:600}.strm-field :deep(.el-input-number){max-width:100%}.plain-rules{padding-left:20px;line-height:1.9;color:var(--muted)}.channel-dir{min-width:0}.channel-dir :deep(.el-input){min-width:0}
@media(max-width:760px){.strm-field{gap:8px}.strm-field>.el-input{min-width:0;flex-basis:100%}.workflow-page :deep(.directory-item span){word-break:break-word}.workflow-page :deep(.task-peek){grid-template-columns:1fr auto}}
</style>
