<script setup lang="ts">
import WorkspaceSections from './WorkspaceSections.vue'
import {useVisiblePolling} from './pageActivity'
import {computed,onMounted,onUnmounted,ref,watch} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import {ElMessage,ElMessageBox} from 'element-plus'
import {VideoPlay,Connection,Bell,DocumentChecked} from '@element-plus/icons-vue'
import SettingCard from './SettingCard.vue'
import ExtensionEditor from './ExtensionEditor.vue'
import {VIDEO_PRESETS,METADATA_PRESETS} from './extensions'
import WorkflowToolbar from './WorkflowToolbar.vue'
import SyncProfiles from './SyncProfiles.vue'
import {copyText} from './clipboard'
import {syncBaselineWithWebhookToken} from './workflowSettings'
const route=useRoute(),router=useRouter()
const section = ref<'tasks' | 'output' | 'maintenance'>('tasks')
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const outputRoot=ref(''),savedBase=ref('')
const config=ref<any>({inherit_video_extensions:true,video_extensions:[],metadata_extensions:[],min_video_size_mb:0,generate_nfo:false,generate_images:false,emby_media_info:false,stale_detect:false,webhook_token:'',deep_delete_dry_run:true,cleanup_empty:true,source_cids:[] as string[],directory_labels:{} as Record<string,string>,notify_emby:true,full_scan:true,cleanup_missing:true}),base=ref('')
const baseline=ref(''),ready=ref(false)
const busy=ref(false),error=ref(''),query=ref('')
const profileEditor=ref<any>(null)
const unidentified=ref<any>(null),selectedUnidentified=ref<string[]>([])
// 按文件夹勾选：后端 groups 返回真实作品目录；只有根目录文件才平铺到 items。
const selectedUnidentifiedDirs=ref<string[]>([])
// 展开某个目录后才看其中的单文件；默认不铺 51 条散文件。
const unidentifiedExpanded=ref<Record<string,boolean>>({})
const visibleUnidentified=computed(()=>{
  const items=unidentified.value?.items||[]
  return items.slice(0,50)
})
function toggleUnidentifiedDir(dir:string){
  unidentifiedExpanded.value={...unidentifiedExpanded.value,[dir]:!unidentifiedExpanded.value[dir]}
}
function selectCurrentUnidentified(){
  selectedUnidentifiedDirs.value=(unidentified.value?.groups||[]).map((g:any)=>g.dir)
  selectedUnidentified.value=[...(unidentified.value?.items||[])]
}

const snapshot=()=>JSON.stringify({config:config.value,base:base.value})
const dirty=computed(()=>ready.value&&snapshot()!==baseline.value)
async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
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
const rows:string[]=[`Emby 挂载前缀：${r.emby_prefix}；预演：${r.dry_run?'开（不会真删）':'关（会真删）'}`]
for(const c of r.cases||[])rows.push(`${c.case}：命中 ${c.managed+c.share} 条`)
rows.push(`通用目录名反例：命中 ${r.generic_directory_matched} 条（正常应为 0）`)
rows.push(r.last_received?`最近一次事件：${r.last_received}`:'最近还没有收到过真实的 Emby 事件')
await ElMessageBox.alert(rows.join('\n'),'深度删除链路自检',{confirmButtonText:'关闭',customClass:'selftest-result'})
})}

const rewriteMsg=ref('')
const cleanupMsg=ref('')
async function loadUnidentified(){await run(async()=>{unidentified.value=await props.api('/strm/unidentified');selectedUnidentified.value=[];selectedUnidentifiedDirs.value=[];unidentifiedExpanded.value={}})}
async function cleanupUnidentified(){
  if(!selectedUnidentified.value.length)return
  try{await ElMessageBox.confirm(`将删除勾选的 ${selectedUnidentified.value.length} 个飞牛本地分享未识别 STRM。会清理空目录，保留网盘源文件、NFO 和图片。`,'清理分享未识别 STRM',{confirmButtonText:'删除所选 STRM',cancelButtonText:'取消',type:'warning'})}catch{return}
  await run(async()=>{
    // 请求体有 64KB 上限，几百条路径一次发会被判「内容过大」；按 100 条一批提交。
    const all=[...selectedUnidentifiedDirs.value,...selectedUnidentified.value],chunk=100
    let removed=0,emptyDirs=0;const failed:string[]=[]
    for(let i=0;i<all.length;i+=chunk){
      const r:any=await props.api('/strm/unidentified/cleanup','POST',{paths:all.slice(i,i+chunk)})
      removed+=r.removed||0;emptyDirs+=r.empty_dirs||0;failed.push(...(r.failed||[]))
    }
    unidentified.value=await props.api('/strm/unidentified');selectedUnidentified.value=[];selectedUnidentifiedDirs.value=[]
    if(failed.length)ElMessage.warning(`已删除 ${removed} 个，${failed.length} 个未删除，请刷新列表检查`)
    else ElMessage.success(`已删除 ${removed} 个本地 STRM，清理 ${emptyDirs} 个空目录`)
  })
}

async function cleanupUnidentifiedEmpty(){
  try{await ElMessageBox.confirm('删除“分享STRM未识别”下的空目录？有文件的目录会保留。','清理空目录',{type:'warning'})}catch{return}
  await run(async()=>{const r:any=await props.api('/strm/unidentified/cleanup','POST',{paths:[]});ElMessage.success(`已清理 ${r.empty_dirs} 个空目录`);unidentified.value=await props.api('/strm/unidentified')})
}

async function cleanupPlaybacks(){await run(async()=>{const r:any=await props.api('/playbacks/cleanup','POST',{});cleanupMsg.value=r.message||`已清理 ${r.removed} 条`;ElMessage.success(cleanupMsg.value)})}
async function rewrite(){await run(async()=>{const p:any=await props.api('/strm/rebuild?dry_run=1','POST',{});if(!p.rewritten&&!p.missing){rewriteMsg.value=`无需重写：${p.unchanged} 条已是最新`;ElMessage.success('本地 STRM 与当前播放地址一致');return}try{await ElMessageBox.confirm(`将重写 ${p.rewritten} 条，缺失 ${p.missing} 条（缺失只报告、不创建）。执行前会自动快照，可一键回滚。`,'按当前地址重写本地 STRM',{confirmButtonText:'开始重写',cancelButtonText:'取消'})}catch{return}const r:any=await props.api('/strm/rebuild','POST',{});rewriteMsg.value=`重写 ${r.rewritten} · 未变 ${r.unchanged} · 缺失 ${r.missing} · 失败 ${r.failed}`;ElMessage.success(r.snapshot?`已重写 ${r.rewritten} 条，快照 #${r.snapshot} 可回滚`:'本地 STRM 已按当前播放地址重写')})}
async function rollbackRewrite(){await run(async()=>{const s:any=await props.api('/strm/rebuild/status');if(!s||!s.snapshot){ElMessage.warning('还没有可回滚的重写记录');return}try{await ElMessageBox.confirm(`把上次重写（快照 #${s.snapshot}）写回的本地 STRM 内容还原？不影响云端文件与播放身份。`,'回滚上次重写',{confirmButtonText:'回滚',cancelButtonText:'取消'})}catch{return}const r:any=await props.api('/strm/rebuild/rollback','POST',{});rewriteMsg.value=`已回滚 ${r.restored}/${r.total} 条（快照 #${r.snapshot}）`;ElMessage.success('已回滚上次重写')})}
async function save(){await run(async()=>{const latest=await props.api('/sync/config');config.value.emby_media_info=!!latest.config.emby_media_info;await props.api('/sync/config','PUT',config.value);try{if(base.value!==savedBase.value){await props.api('/settings','PUT',{playback_base:base.value});savedBase.value=base.value}}catch(e){throw new Error('同步设置已保存；播放地址未保存：'+(e as Error).message)}baseline.value=snapshot();ElMessage.success('STRM 同步设置已保存')})}
async function canLeave(){if(profileEditor.value&&!await profileEditor.value.canLeave())return false;if(!dirty.value)return true;try{await ElMessageBox.confirm('当前修改尚未保存，修改会暂存到当前页面，刷新网页前请记得保存。确定切换？','离开同步设置',{confirmButtonText:'暂存并切换',cancelButtonText:'继续编辑'});return true}catch{return false}}
function beforeUnload(e:BeforeUnloadEvent){if(route.name==='sync'&&(dirty.value||profileEditor.value?.hasDraft())){e.preventDefault();e.returnValue=''}}
defineExpose({canLeave})
onMounted(()=>{window.addEventListener('beforeunload',beforeUnload);run(async()=>{const [data,settings]=await Promise.all([props.api('/sync/config'),props.api('/settings')]);config.value=data.config;outputRoot.value=data.output_root;base.value=settings.playback_base;savedBase.value=base.value;baseline.value=snapshot();ready.value=true})})
onUnmounted(()=>{window.removeEventListener('beforeunload',beforeUnload)})
useVisiblePolling(async()=>{if(!ready.value||busy.value||dirty.value)return;const before=baseline.value,previousBase=savedBase.value;const [data,settings]=await Promise.all([props.api('/sync/config'),props.api('/settings')]);if(busy.value||dirty.value||baseline.value!==before||savedBase.value!==previousBase)return;config.value=data.config;base.value=settings.playback_base;savedBase.value=base.value;baseline.value=snapshot()},5000)
</script>
<template><div class="workflow-page">
<WorkflowToolbar v-model:query="query" title="STRM 管理" save-label="保存全局设置" :dirty="dirty" :busy="busy" :ready="ready" @save="save"/>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>
<WorkspaceSections v-if="!query.trim()" v-model="section" label="STRM 管理分区" :items="[
  {id:'tasks', label:'网盘同步'}, {id:'output', label:'输出设置'}, {id:'maintenance', label:'维护工具'},
]"/>
<div v-show="section==='tasks'||!!query.trim()">
<SettingCard title="同步 115 目录" description="沿用网盘原有路径和文件名生成 STRM。" :summary="outputRoot && '输出目录 '+outputRoot" :icon="VideoPlay" :query="query" keywords="增量 全量 目录 同步 任务 定时 电影 剧集 转存 整理" default-open>
<div class="sync-organize-link">
  <span>需要识别、改名和分类时，使用转存后立即整理。</span>
  <el-button link type="primary" @click="emit('navigate','transfer')">前往资源入库</el-button>
</div>
<SyncProfiles ref="profileEditor" :api="api" @navigate="emit('navigate',$event)"/>
</SettingCard>
</div>
<div v-show="section==='output'||!!query.trim()">
<SettingCard title="播放地址" description="新生成的 STRM 使用这个地址。" :summary="base||'尚未配置播放地址'" :icon="Connection" :query="query" keywords="URL 域名 端口 IP 公网 局域网 302" default-open>
  <el-form label-position="top"><el-form-item label="StrimKeep 播放基础地址"><el-input v-model="base" :disabled="busy" placeholder="http://strimkeep.example.com:9000"/></el-form-item></el-form>
</SettingCard>
<SettingCard title="通知 Emby 入库" description="本批输出有变化后通知 Emby，并检查入库结果。" :summary="config.notify_emby?'自动通知已开启':'自动通知已关闭'" :icon="Bell" :query="query" keywords="通知 Emby 扫描 入库"><div class="workflow-switch-row"><div><strong>同步后通知 Emby</strong><p>通知本批新增、改名和删除路径。核对超时只刷新命中的媒体库。</p></div><el-switch v-model="config.notify_emby" :disabled="busy" aria-label="同步后通知 Emby"/></div></SettingCard>
<SettingCard title="STRM 生成过滤与附属文件" description="与整理格式分开控制；分享 STRM 自动继承这里的生成规则。" :icon="DocumentChecked" :query="query" keywords="扩展名 过滤 视频 元数据 字幕 大小 阈值" default-open>
<div class="workflow-switch-row"><strong>视频扩展名沿用影视整理</strong><el-switch v-model="config.inherit_video_extensions" :disabled="busy"/></div>
<ExtensionEditor v-model="config.video_extensions" label="STRM 视频扩展名" :presets="VIDEO_PRESETS" :max-items="60" :excluded="config.metadata_extensions" :forbidden="['.strm']" :disabled="busy||config.inherit_video_extensions"/>
<p class="hint">开启沿用后，以影视整理保存的扩展名为准；此处显示的是独立设置，关闭沿用后可编辑。未选格式不生成新 STRM，保留原视频和已有输出。</p>
<div class="workflow-switch-row"><div><strong>生成 NFO</strong><p>在 STRM 旁边写电影/单集的 NFO，剧集还会写 tvshow.nfo；Emby 直接读本地元数据，不用再联网刮削。同步老库时也会现场识别补齐，认不出的文件跳过并记录原因。</p></div><el-switch v-model="config.generate_nfo" :disabled="busy"/></div>
<div class="workflow-switch-row"><div><strong>下载海报与背景图</strong><p>从 TMDB 取 poster.jpg / fanart.jpg 放进作品目录，失败只跳过，不影响 STRM。</p></div><el-switch v-model="config.generate_images" :disabled="busy"/></div>
<div class="workflow-switch-row"><div><strong>Emby 媒体信息</strong><p>提取、补写及总开关在「Emby 反代」管理。</p></div><el-button @click="router.push({name:'embyproxy',hash:'#media-info'})">前往设置</el-button></div>
<div class="strm-field"><label>不生成 STRM 的小视频阈值（MB，0 不限制）</label><el-input-number v-model="config.min_video_size_mb" :min="0" :max="1048576" :disabled="busy"/></div>
<ExtensionEditor v-model="config.metadata_extensions" label="实体下载的字幕与元数据扩展名" :presets="METADATA_PRESETS" :max-items="60" :excluded="config.inherit_video_extensions?[]:config.video_extensions" :forbidden="['.strm']" :disabled="busy"/>
<p class="hint">视频只生成链接；这些扩展名实体下载。普通字幕绑定对应视频；其余附属文件增量下载到对应目录，不覆盖已有文件。单个附属文件上限20MB，超限说明并跳过。</p>
</SettingCard>

</div>
<div v-show="section==='maintenance'||!!query.trim()">
<SettingCard title="失效 STRM 清理" :icon="DocumentChecked" :query="query" keywords="自动 本地 同步 清理" default-open>
<div class="workflow-switch-row"><div><strong>同步后清理失效的本地 STRM</strong><p>完整扫描确认源视频已失效后，清理对应的本地输出。</p></div><el-switch v-model="config.cleanup_missing" :disabled="busy" aria-label="同步后清理失效的本地 STRM"/></div>
<div class="workflow-switch-row"><strong>清理失效输出后的空目录</strong><el-switch v-model="config.cleanup_empty" :disabled="busy" aria-label="清理失效输出后的空目录"/></div>
</SettingCard>
<SettingCard title="已有 STRM 的播放地址" description="更换域名或 IP 后，批量更新已登记的本地 STRM。" :icon="Connection" :query="query" keywords="URL 地址 端口 重写 回滚 播放身份">
  <p class="hint">先在「输出设置」保存新地址，再重写已有文件。</p>
  <div class="button-row">
    <el-button :disabled="busy||!base||dirty" @click="rewrite">按当前地址重写全部本地 STRM</el-button>
    <el-button :disabled="busy" @click="rollbackRewrite">回滚上次重写</el-button>
    <el-button :disabled="busy" @click="cleanupPlaybacks">清理未被引用的播放身份</el-button>
  </div>
  <p v-if="rewriteMsg" class="hint">{{rewriteMsg}}</p>
  <p v-if="cleanupMsg" class="hint">{{cleanupMsg}}</p>
</SettingCard>
<SettingCard title="分享未识别 STRM" description="查看本机未识别的分享 STRM，选择不再需要的文件清理。" :summary="unidentified?`共 ${unidentified.total} 个`:'点击查看文件'" :icon="DocumentChecked" :query="query" keywords="STRM 分享 未识别 清理 删除" :default-open="false">
  <div class="button-row"><el-button :disabled="busy" @click="loadUnidentified">查看未识别文件</el-button><el-button :disabled="busy||!unidentified?.items.length" @click="selectCurrentUnidentified">全选当前列表</el-button><el-button :disabled="busy||!selectedUnidentified.length" @click="selectedUnidentified=[]">取消选择</el-button><el-button :disabled="busy||!unidentified||!unidentified.empty_total" @click="cleanupUnidentifiedEmpty">清理空目录（{{unidentified?.empty_total||0}}）</el-button><el-button type="danger" plain :disabled="busy||!selectedUnidentified.length" @click="cleanupUnidentified">清理所选（{{selectedUnidentified.length}}）</el-button></div>
  <p class="hint">删除勾选的本地 STRM，并清理空目录；保留网盘源文件、NFO 和图片。未识别文件可能是本程序生成的，不代表外部文件。</p>
  <template v-if="unidentified"><p class="hint">{{`未识别 STRM ${unidentified.total} 个，空目录 ${unidentified.empty_total||0} 个`}}</p><p v-if="unidentified.total>unidentified.items.length" class="hint">每次最多显示 500 个，清理后可再次查看。</p><details v-if="unidentified.empty_total"><summary>查看空目录（{{unidentified.empty_total}} 个）</summary><p v-for="path in unidentified.empty_dirs" :key="path" class="hint">{{path}}</p></details><div v-if="unidentified.groups?.length" class="directory-item"><span class="hint">按文件夹（勾选整个目录一起清理）：</span></div><div v-for="group in unidentified.groups" :key="group.dir" class="unidentified-group"><div class="directory-item"><el-checkbox v-model="selectedUnidentifiedDirs" :value="group.dir" :disabled="busy">{{group.dir}}（{{group.count}} 个）</el-checkbox><el-button link type="primary" @click="toggleUnidentifiedDir(group.dir)">{{unidentifiedExpanded[group.dir]?'收起文件':'展开文件'}}</el-button></div><div v-if="unidentifiedExpanded[group.dir]" class="unidentified-children"><div v-for="path in group.items" :key="path" class="directory-item"><el-checkbox v-model="selectedUnidentified" :value="path" :disabled="busy">{{path}}</el-checkbox></div></div></div><p v-if="unidentified.items.length" class="hint">根目录平铺文件（{{unidentified.items.length}}）：</p><div v-for="path in visibleUnidentified" :key="path" class="directory-item"><el-checkbox v-model="selectedUnidentified" :value="path" :disabled="busy">{{path}}</el-checkbox></div></template>
</SettingCard>
<SettingCard title="深度删除（Emby Webhook 联动）" description="配置 Emby 删除事件的联动与预演。" :summary="config.webhook_token?'令牌已生成':'尚未生成令牌'" :icon="Connection" :query="query" keywords="Webhook 删除 令牌 预演 Emby" :default-open="false"><p class="hint" style="margin:0 0 12px">在 Emby 里删除电影/单集时自动删除网盘文件（进回收站可还原）和本地 strm。需要配置 <b>Emby 的 Webhook 插件</b>（不是第三方插件的通知），按以下步骤配置：</p><el-form label-position="top" :disabled="busy"><el-form-item label="第一步：点「生成令牌」，点完自动保存"><div class="channel-dir"><el-button type="primary" :disabled="busy||!ready" @click="genToken">{{config.webhook_token?'重新生成':'生成令牌'}}</el-button><el-input v-model="config.webhook_token" readonly style="max-width:260px" placeholder="点生成"/><span v-if="config.webhook_token" class="hint" style="margin:0">✓ 已自动保存</span></div></el-form-item><el-form-item label="第二步：复制下方 Webhook 地址"><div class="channel-dir"><el-input :model-value="webhookUrl" type="textarea" :rows="3" readonly aria-label="完整 Webhook 地址" placeholder="配置播放地址后自动出现"/><el-button :disabled="!webhookUrl" @click="copyUrl">一键复制</el-button></div><p class="hint" style="margin:4px 0 0">自动使用上方配置的播放地址（Emby 播放 strm 时就在用这个地址，必然可达）。内网填 <code>http://你的NAS地址:9000</code>，公网填域名——配好播放地址后这里自动跟上，无需手动改。</p></el-form-item><el-form-item label="第三步：在 Emby 里配置 Webhook 通知"><div style="font-size:13px;line-height:2;color:var(--text)"><p style="margin:0">Emby 控制台 → 通知 → 添加通知 → 选 <b>Webhook</b>：</p><p style="margin:2px 0 0">① URL 粘贴上面复制的地址<br/>② 请求方法：<b>POST</b><br/>③ 内容类型：<b>application/json</b>（不要选 multipart）<br/>④ 事件类型只勾：<b>Item Removed（项目已删除）</b><br/>⑤ 保存</p></div></el-form-item><el-form-item label="第四步：预演验证（先开着，确认无误后关闭）"><el-switch v-model="config.deep_delete_dry_run"/><p class="hint" style="margin:4px 0 0">预演只记录将删什么。去 Emby 删个测试文件，实时日志里看到命中正确后关掉再保存。</p></el-form-item><el-form-item label="第五步（可选）：链路自检"><div class="channel-dir"><el-button :disabled="busy" @click="selftest">自检</el-button><span class="hint" style="margin:0">不碰 Emby 也能验证：用台账里的真实路径跑一遍匹配，只读、永远不删</span></div></el-form-item></el-form><p class="hint">之后你在 Emby 里删除电影、单集、整季或整部剧集，Emby Webhook 就会发事件过来：网盘库 → 本地 strm + NFO 删除 + 网盘文件进 115 回收站（可还原）；分享库 → 只删本地 strm。整理或转存运行时后台队列会自动让路，等它们结束再继续删。</p></SettingCard>
</div>
<p v-if="query.trim()" class="search-caption">仅显示匹配的设置卡片；清空搜索可查看全部。</p>
</div></template>

<style scoped>.strm-field{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:12px 0;min-width:0}.strm-field label{width:100%;font-size:13px}.strm-field :deep(.el-select){width:100%;min-width:0}.strm-field>.el-input{flex:1;min-width:160px}</style>

<style scoped>

.sync-organize-link{display:flex;align-items:center;flex-wrap:wrap;gap:2px 12px;color:var(--muted);font-size:13px}
.workflow-page :deep(.directory-item){align-items:center}.workflow-page :deep(.directory-group h3){font-size:13px}.strm-field{padding:2px 0}.strm-field label{color:var(--text);font-weight:600}.strm-field :deep(.el-input-number){max-width:100%}.plain-rules{padding-left:20px;line-height:1.9;color:var(--muted)}.channel-dir{min-width:0}.channel-dir :deep(.el-input){min-width:0}
@media(max-width:760px){.strm-field{gap:8px}.strm-field>.el-input{min-width:0;flex-basis:100%}.workflow-page :deep(.directory-item span){word-break:break-word}.workflow-page :deep(.task-peek){grid-template-columns:1fr auto}}
</style>
