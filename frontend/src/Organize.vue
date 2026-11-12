<script setup lang="ts">
import {useVisiblePolling} from './pageActivity'
import {ref,onMounted,computed,onUnmounted} from 'vue'
import {useRoute} from 'vue-router'
import {ElMessage,ElMessageBox} from 'element-plus'
import {FolderOpened,VideoPlay,Timer,Refresh,Bell,Setting} from '@element-plus/icons-vue'
import DirectoryPicker from './DirectoryPicker.vue'
import SettingCard from './SettingCard.vue'
import ExtensionEditor from './ExtensionEditor.vue'
import {VIDEO_PRESETS,SUBTITLE_PRESETS} from './extensions'
import WorkflowToolbar from './WorkflowToolbar.vue'
import AutomationSettings from './AutomationSettings.vue'
import {taskPresentation} from './taskPresentation'
import Upgrade from './Upgrade.vue'
import EmbyMapping from './EmbyMapping.vue'
import {scheduleDraft,schedulePatch,scheduleIdentity,type ScheduleDraft} from './workflowSettings'
const route=useRoute()
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const config=ref<any>({video_extensions:['.avi','.m2ts','.m4v','.mkv','.mov','.mp4','.ts','.webm'],subtitle_extensions:['.ass','.idx','.srt','.ssa','.sub','.sup','.vtt'],min_video_size_mb:0,recycle_small_videos:false,ignore_keywords:[],rename_enabled:true,folder_language:'zh',filename_language:'zh',ffprobe_enabled:true,probe_prefetch:true,probe_concurrency:2,identify_prefetch:true,classify_enabled:true,wash_enabled:true,children_enabled:true,concert_enabled:true,region_enabled:true,year_enabled:false,cleanup_empty_enabled:true,source_cids:[] as string[],archive_cid:'',pending_cid:'',directory_labels:{} as Record<string,string>,retries:1,retry_interval:2,strm_delay:1.5,notify_emby:true})
const schedule=ref<ScheduleDraft>(scheduleDraft({},'scan')),baseline=ref(''),scheduleBaseline=ref(''),ready=ref(false),configured=ref(false)
const picker=ref(''),busy=ref(false),error=ref(''),query=ref(''),nextDue=ref<number>(),latest=ref<any>(null),policySummary=ref('正在读取洗版规则'),ruleEditor=ref<any>(null)

const dirty=computed(()=>ready.value&&(JSON.stringify(config.value)!==baseline.value||scheduleIdentity(schedule.value,'scan')!==scheduleBaseline.value))
const label=(id:string)=>config.value.directory_labels[id]||id
async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function picked(folder:{id:string;name:string}){config.value.directory_labels[folder.id]=folder.name;if(picker.value==='source'){if(!config.value.source_cids.includes(folder.id))config.value.source_cids.push(folder.id)}else if(picker.value==='archive')config.value.archive_cid=folder.id;else config.value.pending_cid=folder.id;picker.value=''}
async function refreshJob(){const [jobs,timers]=await Promise.all([props.api('/organize/jobs'),props.api('/system/schedules')]);latest.value=jobs.items.find((j:any)=>j.kind==='organize')||null;nextDue.value=timers.states.find((s:any)=>s.kind==='organize')?.next_due}
async function refreshRules(){const p=(await props.api('/upgrade/config')).config;policySummary.value=({pending:'冲突转待处理',skip:'冲突原位置跳过',upgrade:'允许洗版'} as Record<string,string>)[p.mode]+` · 已启用 ${p.rules.filter((r:any)=>r.enabled).length}/${p.rules.length} 项规则 · `+([p.resolution_groups?'分辨率共存':'',p.dynamic_groups!=='off'?'动态范围共存':'',p.fps_groups?'帧率共存':''].filter(Boolean).join(' / ')||'单版本选优')}
async function save(){await run(async()=>{await props.api('/organize/config','PUT',config.value);try{if(scheduleIdentity(schedule.value,'scan')!==scheduleBaseline.value){const r=await props.api('/system/schedules','PUT',schedulePatch('scan',schedule.value));schedule.value=scheduleDraft(r.config,'scan');nextDue.value=r.states.find((s:any)=>s.kind==='organize')?.next_due}}catch(e){throw new Error('目录与整理设置已保存；定时设置未保存：'+(e as Error).message)}baseline.value=JSON.stringify(config.value);scheduleBaseline.value=scheduleIdentity(schedule.value,'scan');configured.value=true;ElMessage.success('整理设置已保存')})}
async function start(){if(dirty.value)return;await run(async()=>{const job=await props.api('/organize/jobs','POST');latest.value=job;ElMessage.success(job.merged?`相同任务已在等待，已合并到 #${job.id}`:`整理任务 #${job.id} 已加入队列`)})}
async function canLeave(){if(!dirty.value)return true;try{await ElMessageBox.confirm('当前修改尚未保存，修改会暂存到当前页面，刷新网页前请记得保存。确定切换？','离开整理设置',{confirmButtonText:'暂存并切换',cancelButtonText:'继续编辑'});return true}catch{return false}}
function beforeUnload(e:BeforeUnloadEvent){if(dirty.value&&route.name==='organize'){e.preventDefault();e.returnValue=''}}
defineExpose({canLeave})
onMounted(()=>{window.addEventListener('beforeunload',beforeUnload);run(async()=>{const [data,timers]=await Promise.all([props.api('/organize/config'),props.api('/system/schedules')]);config.value=data.config;schedule.value=scheduleDraft(timers.config,'scan');nextDue.value=timers.states.find((s:any)=>s.kind==='organize')?.next_due;baseline.value=JSON.stringify(config.value);scheduleBaseline.value=scheduleIdentity(schedule.value,'scan');configured.value=!!config.value.source_cids.length&&!!config.value.archive_cid;ready.value=true;await Promise.all([refreshJob(),refreshRules()])})})
onUnmounted(()=>{window.removeEventListener('beforeunload',beforeUnload)})
useVisiblePolling(async()=>{if(!busy.value)await refreshJob()},5000)
</script>
<template>
<div class="workflow-page">
<WorkflowToolbar v-model:query="query" title="影视整理" :dirty="dirty" :busy="busy" :ready="ready" @save="save"/>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>
<SettingCard title="整理执行" description="手动执行与定时扫描都在这里。" :summary="schedule.enabled?`定时已开启 · ${schedule.mode==='cron'?schedule.cron:'每 '+schedule.minutes+' 分钟'}`:'定时已关闭 · 可手动执行'" :icon="VideoPlay" :query="query" keywords="立即扫描 自动 定时 任务 间隔" :default-open="false">
<div class="execution-row"><div><strong>扫描 → 识别 → 归档 → 生成 STRM</strong><p>按保存的目录和洗版规则执行，任务依次排队。</p></div><el-button type="primary" :icon="VideoPlay" :disabled="busy||!ready||!configured||dirty" @click="start">立即整理</el-button></div>
<p v-if="dirty" class="inline-notice">请先保存修改，再开始整理。</p><p v-else-if="ready&&!configured" class="inline-notice">先在下方选择目录，再点击顶部保存修改。</p>
<AutomationSettings v-model="schedule" kind="scan" :busy="busy" :next-due="nextDue" :waiting="schedule.mode==='interval'&&['queued','running','waiting_login'].includes(latest?.state)"/>
<div v-if="latest" class="task-peek"><span>最近任务 #{{latest.id}}</span><strong>{{taskPresentation(latest).title}}</strong><el-button text type="primary" @click="emit('navigate','tasks')">查看任务进度 →</el-button></div>
</SettingCard>
<SettingCard title="扫描来源与归档目录" description="选择从哪里整理，以及整理后存到哪里。" :summary="`${config.source_cids.length} 个待整理目录 · ${config.archive_cid?'已选归档目录':'待选择归档目录'}`" :icon="FolderOpened" :query="query" keywords="来源 源目录 目标 分类 115" :default-open="false">
<div class="directory-group"><h3>待整理目录 <small>可选择多个</small></h3><div v-for="id in config.source_cids" :key="id" class="directory-item"><el-icon><FolderOpened/></el-icon><span>{{label(id)}}</span><el-button text :disabled="busy" @click="config.source_cids=config.source_cids.filter((x:string)=>x!==id)">移除</el-button></div><el-button :disabled="busy" @click="picker='source'">添加待整理目录</el-button></div>
<div class="directory-group"><h3>归档根目录 <small>只选择一个</small></h3><p class="selected-directory">{{config.archive_cid?label(config.archive_cid):'尚未选择'}}</p><el-button :disabled="busy" @click="picker='archive'">选择归档目录</el-button></div>
<p class="compact-note">待整理、归档和已配置的未识别目录须相互独立，不能相同或互相包含。</p>
</SettingCard>
<SettingCard title="未识别目录（可选）" description="识别失败时保留原目录与附属文件，整体分流。" :summary="config.pending_cid?label(config.pending_cid):'未配置，整理失败原位保留'" :icon="FolderOpened" :query="query" keywords="未识别 失败 歧义 待处理" :default-open="false"><p class="selected-directory">{{config.pending_cid?label(config.pending_cid):'尚未选择'}}</p><el-button :disabled="busy" @click="picker='pending'">选择未识别目录</el-button><el-button v-if="config.pending_cid" :disabled="busy" @click="config.pending_cid=''">清除选择</el-button><p class="hint">未配置时：失败文件原位保留，不改名、不移动。配置后：保留原目录层级、视频及附属文件，整体移入此目录；同名冲突原位保留。来源根目录中的散装视频保留原名转入。</p></SettingCard>
<SettingCard title="需要处理的文件类型" description="显示、添加或移除参与整理的视频和字幕扩展名。" :icon="Setting" :query="query" keywords="扩展名 视频 字幕 ISO 格式 过滤" :default-open="false">
<ExtensionEditor v-model="config.video_extensions" label="视频扩展名" :presets="VIDEO_PRESETS" :max-items="40" :excluded="config.subtitle_extensions" :disabled="busy"/>
<ExtensionEditor v-model="config.subtitle_extensions" label="字幕扩展名" :presets="SUBTITLE_PRESETS" :allowed="SUBTITLE_PRESETS" :max-items="20" :excluded="config.video_extensions" :disabled="busy"/>
<p class="hint">扩展名不区分大小写；字幕限于支持的类型。没收进来的格式会原位保留并写明跳过原因；字幕只跟随对应视频，不受大小过滤影响。需要处理哪种格式就把它加进列表（例如 ISO 原盘），和其他视频走同一条流程；探测读不出来时自动按文件名解析规格，能不能播取决于播放器。</p>
</SettingCard>
<SettingCard title="重命名策略" description="分别设置新作品目录与视频文件的名称语言。" :icon="Setting" :query="query" keywords="重命名 中文 英文 语言 文件名 目录名" :default-open="false">
<div class="workflow-switch-row"><div><strong>重命名视频与对应字幕</strong><p>关闭后保留原视频文件名，识别与归档仍执行。</p></div><el-switch v-model="config.rename_enabled" :disabled="busy"/></div>
<div class="organize-option"><label>新作品文件夹名称</label><el-select v-model="config.folder_language" :disabled="busy"><el-option label="优先中文" value="zh"/><el-option label="优先英文" value="en"/></el-select></div>
<div class="organize-option"><label>视频文件中的作品名称</label><el-select v-model="config.filename_language" :disabled="busy||!config.rename_enabled"><el-option label="优先中文" value="zh"/><el-option label="优先英文" value="en"/></el-select></div>
<p class="hint">使用 TMDB 对应语言主名，中文不足时查询中文别名，最后回退作品原名。年份、TMDB ID、季集和技术规格独立生成；已有作品目录与作品名保持稳定。</p>
<div class="workflow-switch-row"><div><strong>提取媒体信息完善命名</strong><p>开启后探测实际规格；关闭后从文件名识别。探测失败时自动按文件名解析，不会中断整理。</p></div><el-switch v-model="config.ffprobe_enabled" :disabled="busy"/></div>
<div class="workflow-switch-row"><div><strong>批量探测预取</strong><p>整理前按并发预取媒体信息，主流程逐条命中缓存不再等待远程探测；单个文件失败只跳过它，其余照常处理。</p></div><el-switch v-model="config.probe_prefetch" :disabled="busy||!config.ffprobe_enabled"/></div>
<div class="workflow-switch-row"><div><strong>批量识别预取</strong><p>开工前并发做完 TMDB 搜索，逐条处理直接命中缓存；只并发读，改名和移动仍按顺序来。</p></div><el-switch v-model="config.identify_prefetch" :disabled="busy"/></div>
<div class="organize-option" v-if="config.probe_prefetch"><label>预取并发数</label><el-input-number v-model="config.probe_concurrency" :min="1" :max="4" :disabled="busy||!config.ffprobe_enabled"/></div>
<p class="hint" v-if="config.probe_prefetch">并发越高探得越快，但会更集中地占用 115 直链带宽；默认 2，出问题时先降为 1。</p>
</SettingCard>
<SettingCard title="忽略策略" description="命中条件的视频原位保留、自动跳过，不转人工核实。" :icon="Setting" :query="query" keywords="忽略 关键词 小文件 大小 MB 过滤" :default-open="false">
<div class="organize-option"><label>最小视频大小（MB，0 表示关闭）</label><el-input-number v-model="config.min_video_size_mb" :min="0" :max="1048576" :disabled="busy"/></div>
<div class="workflow-switch-row"><strong>低于阈值的视频直接回收</strong><el-switch v-model="config.recycle_small_videos" :disabled="busy||!config.min_video_size_mb"/></div>
<p class="hint">默认关闭：只跳过、不删除。打开后，小于上面阈值的视频会被回收（115 回收站可恢复），适合清理宣传片/广告。</p>
<div class="organize-option"><label>忽略关键词（输入后按回车，可添加多个）</label><el-select v-model="config.ignore_keywords" multiple filterable allow-create default-first-option :disabled="busy" placeholder="例如 sample、预告片"/></div>
<p class="hint">关键词按视频文件名匹配，不区分大小写；字幕不受大小阈值影响。这里的忽略不会删除文件。</p>
</SettingCard>
<SettingCard title="分类策略" description="配置新归档的分类层级；修改不会批量搬动已有片库。" :icon="FolderOpened" :query="query" keywords="分类 儿童 演唱会 地区 年份" :default-open="false">
<div class="workflow-switch-row"><strong>自动分类</strong><el-switch v-model="config.classify_enabled" :disabled="busy"/></div>
<div class="workflow-switch-row"><strong>儿童节目独立分类</strong><el-switch v-model="config.children_enabled" :disabled="busy||!config.classify_enabled"/></div>
<div class="workflow-switch-row"><strong>演唱会独立分类</strong><el-switch v-model="config.concert_enabled" :disabled="busy||!config.classify_enabled"/></div>
<div class="workflow-switch-row"><strong>按地区二次分类</strong><el-switch v-model="config.region_enabled" :disabled="busy||!config.classify_enabled"/></div>
<div class="workflow-switch-row"><strong>按年份增加目录层级</strong><el-switch v-model="config.year_enabled" :disabled="busy"/></div>
<p class="hint">关闭自动分类后按电影/剧集归档，仍保留作品目录和季目录。音乐类电影须有明确演唱会证据，否则保留待处理，不把所有音乐片当演唱会。</p>
</SettingCard>
<SettingCard title="清理开关" description="只影响本任务关联的处理范围；洗版在「洗版与多版本」里选。" :icon="Refresh" :query="query" keywords="清理 空目录 洗版 开关 删除" :default-open="false">
<div class="workflow-switch-row"><div><strong>整理后清理关联空目录</strong><p>保留扫描根、含媒体或未知文件的目录；不删除忽略的视频。</p></div><el-switch v-model="config.cleanup_empty_enabled" :disabled="busy"/></div>
</SettingCard>
<SettingCard title="洗版与多版本" description="设置同片、同集的处理方式和九项比较顺序。" :summary="policySummary" :icon="Refresh" :query="query" keywords="发布组 资源类型 分辨率 动态范围 帧率 色深 音频 编码 大小 杜比" :default-open="false"><p class="hint">发布组、资源类型、分辨率、动态范围、帧率、色深、音频、编码、文件大小；可按分辨率、动态范围和帧率共存分组。</p><div class="inline-rules"><Upgrade ref="ruleEditor" :api="api"/></div></SettingCard>
<SettingCard title="STRM 与 Emby 通知" description="整理成功后生成 STRM、同步绑定字幕。" :summary="config.notify_emby?'有成功输出后通知 Emby 并检查入库':'自动通知 Emby 已关闭'" :icon="Bell" :query="query" keywords="联动 入库 字幕 Emby STRM" :default-open="false"><div class="workflow-switch-row"><div><strong>整理后通知 Emby 入库</strong><p>按本批文件路径通知 Emby；核对超时只刷新命中的目标库。</p></div><el-switch v-model="config.notify_emby" :disabled="busy" aria-label="整理后通知 Emby"/></div><p class="hint">本地输出为 STRM 和绑定字幕；NFO 与图片由 Emby 管理。下方映射用于发送准确的 Emby 入库路径。</p><el-button :disabled="busy" @click="emit('navigate','connections')">配置 Emby 与播放地址</el-button></SettingCard>
<SettingCard title="整理联动" description="整理完成后，按设置的延迟生成 STRM。" :icon="Setting" :query="query" keywords="联动 延迟 STRM FFprobe 缓存" :default-open="false"><div class="workflow-field-row"><label>生成 STRM 延迟（秒）</label><el-input-number v-model="config.strm_delay" :min="0" :max="60" :step="0.5" :precision="1" :disabled="busy" aria-label="生成 STRM 延迟（秒）"/></div><p class="hint">支持 1.5 秒等小数值。直接扫描待整理目录，读取当前作品目录比较版本，复用已有探测信息。</p></SettingCard>
<p v-if="query" class="search-caption">仅显示匹配的设置卡片；清空搜索可查看全部。</p>
<section class="panel"><h2>Emby 路径映射</h2><EmbyMapping :api="api"/></section>
<DirectoryPicker v-if="picker" :api="api" @select="picked" @close="picker=''"/>
</div>
</template>
<style scoped>
.ext-row{align-items:flex-start}
.ext-chips{display:flex;flex-wrap:wrap;gap:6px;min-width:0}
.ext-chips :deep(.el-check-tag){font-size:12px;padding:5px 10px;border-radius:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.ext-chips :deep(.el-check-tag.is-checked){font-weight:500}
.ext-add{display:flex;gap:8px;align-items:center}

.organize-option{display:grid;gap:8px;margin:14px 0}.organize-option :deep(.el-select){width:100%;min-width:0}.organize-option label{font-size:13px}.inline-rules{margin-top:16px;padding-top:16px;border-top:1px solid var(--line,#e6ebf2)}
.inline-rules :deep(.panel){margin-bottom:16px}
</style>

<style scoped>

.workflow-page :deep(.directory-group){padding:16px 0}.workflow-page :deep(.directory-group h3){font-size:13px}.workflow-page :deep(.directory-item){border-radius:8px;align-items:center}.workflow-page :deep(.compact-note){line-height:1.7}.workflow-page :deep(.ext-chips){gap:7px}.workflow-page :deep(.inline-notice){padding:10px 12px;border-radius:7px;line-height:1.6}.workflow-page :deep(.task-peek){padding-top:15px}.workflow-page :deep(.organize-option){gap:12px}
@media(max-width:760px){.workflow-page :deep(.directory-item span){word-break:break-word}.workflow-page :deep(.organize-option){align-items:stretch}.workflow-page :deep(.ext-add){flex-wrap:wrap}.workflow-page :deep(.task-peek){grid-template-columns:1fr auto}}
</style>
