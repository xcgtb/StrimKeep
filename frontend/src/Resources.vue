<script setup lang="ts">
import PageHeader from './PageHeader.vue'
import DirectoryPicker from './DirectoryPicker.vue'
import {useVisiblePolling} from './pageActivity'
import {ref,computed,onMounted,watch} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import {copyText} from './clipboard'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>;embedded?:boolean;view?:'inbox'|'sources'}>()
const emit=defineEmits<{navigate:[page:string]}>()

const tab=ref<'inbox'|'sources'>(props.view||'inbox')
watch(()=>props.view,value=>{if(value)tab.value=value})
const config=ref<any>({target_cid:'',target_label:'',auto_organize:false})
const webhookToken=ref('')
const text=ref(''),title=ref(''),auto=ref(false)
const target=ref({cid:'',label:''}),picker=ref<''|'manual'|'config'|'feed'>('')
const inbox=ref<any[]>([]),counts=ref<Record<string,number>>({}),status=ref('all'),source=ref('')
const feeds=ref<any[]>([]),channels=ref<any[]>([]),editing=ref(0)
const blankFeed=()=>({name:'',url:'',enabled:true,interval:300,target_cid:'',include:'',exclude:'',auto_organize:false})
const feed=ref<any>(blankFeed())
const rssXml=ref(''),importing=ref(false)
const busy=ref(false),loading=ref(false),error=ref('')
const STATUS:Record<string,string>={pending:'待处理',queued:'已入队',duplicate:'重复',needs_review:'待核查',failed:'失败',skipped:'已跳过'}
const SOURCE:Record<string,string>={manual:'手动',rss:'RSS',webhook:'Webhook',telegram:'Telegram'}
const statusType=(s:string):'success'|'danger'|'warning'|'info'=>s==='queued'?'success':s==='failed'?'danger':s==='needs_review'?'warning':'info'
const webhookUrl=computed(()=>webhookToken.value?`${location.origin}/api/v1/resources/webhook/${webhookToken.value}`:'')
const feedHealth=computed(()=>feeds.value.reduce((acc:any,row:any)=>{if(row.error||row.failures)acc.bad++;else acc.ok++;return acc},{ok:0,bad:0}))
function fmtTime(ts:number){return ts?new Date(ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'}

async function loadConfig(){try{const r=await props.api('/resources/config');config.value=r.config||config.value;webhookToken.value=r.webhook_token||''}catch(e){error.value=(e as Error).message}}
async function loadInbox(){try{const params=new URLSearchParams({status:status.value,limit:'100'});if(source.value)params.set('source',source.value);const r=await props.api('/resources/inbox?'+params.toString());inbox.value=r.items||[];counts.value=r.counts||{}}catch(e){error.value=(e as Error).message}}
async function loadFeeds(){try{feeds.value=(await props.api('/resources/feeds')).items||[]}catch(e){error.value=(e as Error).message}}
async function loadSources(){try{const r=await props.api('/resources/sources');channels.value=r.channels||[];if(r.feeds)feeds.value=r.feeds}catch(e){error.value=(e as Error).message}}
async function load(){loading.value=true;error.value='';try{await Promise.all([loadConfig(),loadInbox(),loadFeeds(),loadSources()])}finally{loading.value=false}}
onMounted(load)
useVisiblePolling(loadInbox,5000)
function openPicker(where:'manual'|'config'|'feed'){picker.value=where}
function picked(folder:{id:string;name:string}){
  if(picker.value==='manual')target.value={cid:folder.id,label:folder.name}
  else if(picker.value==='config')config.value={...config.value,target_cid:folder.id,target_label:folder.name}
  else feed.value.target_cid=folder.id
  picker.value=''
}
async function submit(){
  if(busy.value)return
  const dest=target.value.cid||config.value.target_cid
  if(!dest){ElMessage.warning('请先选择默认目录，或给这次提交指定目录');return}
  busy.value=true;error.value=''
  try{
    const r=await props.api('/resources/inbox','POST',{text:text.value,title:title.value,target_cid:target.value.cid,auto_organize:auto.value})
    ElMessage.success(`已接收 ${r.accepted||0} 条${r.duplicates?`，跳过重复 ${r.duplicates} 条`:''}`)
    text.value='';title.value='';await loadInbox()
  }catch(e){error.value=(e as Error).message}finally{busy.value=false}
}
async function saveConfig(){busy.value=true;try{const r=await props.api('/resources/config','PUT',config.value);config.value=r.config||config.value;webhookToken.value=r.webhook_token||'';ElMessage.success('默认设置已保存')}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function rotate(){busy.value=true;try{webhookToken.value=(await props.api('/resources/webhook-token','POST')).token||'';ElMessage.success('Webhook 令牌已更新')}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function copyWebhook(){try{await copyText(webhookUrl.value,navigator.clipboard,document);ElMessage.success('Webhook 地址已复制')}catch(e){ElMessage.error((e as Error).message)}}
async function retry(row:any){busy.value=true;try{await props.api(`/resources/inbox/${row.id}/retry`,'POST');await loadInbox()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function skip(row:any){busy.value=true;try{await props.api(`/resources/inbox/${row.id}/skip`,'POST');await loadInbox()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function remove(row:any){try{await ElMessageBox.confirm(`删除收件箱条目「${row.title||row.id}」？不会删除已经入队的任务。`,'删除条目',{confirmButtonText:'删除',cancelButtonText:'取消',type:'warning'})}catch{return}busy.value=true;try{await props.api(`/resources/inbox/${row.id}`,'DELETE');await loadInbox()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function saveFeed(){busy.value=true;try{const body={...feed.value};if(editing.value)await props.api(`/resources/feeds/${editing.value}`,'PUT',body);else await props.api('/resources/feeds','POST',body);editing.value=0;feed.value=blankFeed();await loadFeeds();ElMessage.success('RSS 源已保存')}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function editFeed(row:any){editing.value=row.id;feed.value={name:row.name,url:row.url,enabled:!!row.enabled,interval:row.interval,target_cid:row.target_cid,include:row.include,exclude:row.exclude,auto_organize:!!row.auto_organize}}
function cancelFeed(){editing.value=0;feed.value=blankFeed()}
async function runFeed(row:any){busy.value=true;try{await props.api(`/resources/feeds/${row.id}/run`,'POST');await loadFeeds();ElMessage.success('已安排本轮拉取')}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function importRssXml(){if(!rssXml.value.trim()){ElMessage.warning('先粘贴 RSS XML 内容');return}importing.value=true;error.value='';try{const target=feed.value.target_cid||config.value.target_cid;const r=await props.api('/resources/feeds/import','POST',{xml:rssXml.value,target_cid:target,auto_organize:!!feed.value.auto_organize});ElMessage.success(`已解析 ${r.items||0} 条，接收 ${r.accepted||0} 条${r.duplicates?`，重复 ${r.duplicates} 条`:''}`);rssXml.value='';await loadInbox()}catch(e){error.value=(e as Error).message}finally{importing.value=false}}
async function removeFeed(row:any){try{await ElMessageBox.confirm(`删除 RSS 源「${row.name||row.url}」？`,'删除 RSS 源',{confirmButtonText:'删除',cancelButtonText:'取消',type:'warning'})}catch{return}busy.value=true;try{await props.api(`/resources/feeds/${row.id}`,'DELETE');await loadFeeds()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
</script>
<template>
<PageHeader v-if="!props.embedded" title="资源接入" description="手动、RSS、Webhook 先进入收件箱，去重后复用现有转存/离线/整理链路。">
  <template #actions><div class="button-row"><el-button :loading="loading" @click="load">刷新</el-button><el-button type="primary" @click="emit('navigate','transfer')">资源入库 →</el-button></div></template>
</PageHeader>
<el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="resource-alert"/>

<el-tabs v-model="tab" class="workspace-tabs" :class="{'embedded-tabs':props.embedded}">
<el-tab-pane label="收件箱" name="inbox">
 <p v-if="props.embedded" class="hint resource-embedded-hint">RSS / Webhook 自动进入这里；手动链接请用下方「立即提交」，避免两套输入框。</p>
 <section v-if="!props.embedded" class="panel resource-compose">
  <div class="resource-compose-head"><div><h2>粘贴资源</h2><p class="hint">支持一次粘贴多个 115 分享、磁力、ED2K、直链；相同资源自动去重。</p></div><el-tag type="info">待处理 {{counts.pending||0}} · 已入队 {{counts.queued||0}}</el-tag></div>
  <el-input v-model="text" type="textarea" :rows="4" maxlength="16000" show-word-limit placeholder="https://115.com/s/... 或 magnet:?xt=urn:btih:...&#10;可一次粘贴多行"/>
  <div class="resource-form-row"><el-input v-model="title" placeholder="备注标题（可选）" maxlength="300"/><el-button @click="openPicker('manual')">{{target.label||'选择本次落点'}}</el-button><el-switch v-model="auto" active-text="落盘后整理"/><el-button type="primary" :loading="busy" :disabled="!text.trim()" @click="submit">加入收件箱</el-button></div>
  <p class="hint">未选本次落点时使用默认目录：{{config.target_label||config.target_cid||'未设置'}}。</p>
 </section>

 <section class="panel resource-inbox">
  <div class="resource-toolbar"><div class="button-row"><el-select v-model="status" style="width:130px" @change="loadInbox"><el-option label="全部状态" value="all"/><el-option v-for="(label,key) in STATUS" :key="key" :label="label" :value="key"/></el-select><el-select v-model="source" clearable placeholder="全部来源" style="width:130px" @change="loadInbox"><el-option v-for="(label,key) in SOURCE" :key="key" :label="label" :value="key"/></el-select></div><span class="hint">共 {{inbox.length}} 条</span></div>
  <el-empty v-if="!inbox.length&&!loading" description="收件箱还是空的"/>
  <div v-else class="resource-rows">
   <article v-for="row in inbox" :key="row.id" class="resource-row">
    <div class="resource-row-main"><div class="resource-row-title"><el-tag size="small" :type="statusType(row.status)">{{STATUS[row.status]||row.status}}</el-tag><strong>{{row.title||('资源 #'+row.id)}}</strong><span class="muted">{{SOURCE[row.source]||row.source}}</span></div><p class="resource-text">{{row.text}}</p><p class="hint">{{row.message||'等待 Worker 处理'}}<span v-if="row.job_ids?.length"> · 任务 {{row.job_ids.join(', ')}}</span></p></div>
    <div class="resource-row-actions"><el-button size="small" :disabled="busy||row.status==='queued'" @click="retry(row)">重试</el-button><el-button size="small" :disabled="busy||row.status==='queued'" @click="skip(row)">跳过</el-button><el-button size="small" text type="danger" :disabled="busy" @click="remove(row)">删除</el-button></div>
   </article>
  </div>
 </section>
</el-tab-pane>


<el-tab-pane label="来源与设置" name="sources">
 <section class="panel resource-settings">
  <div class="resource-compose-head"><div><h2>默认落点</h2><p class="hint">RSS/Webhook 未指定目录时使用；手动链接用下方「保存目录」。</p></div><el-button @click="openPicker('config')">{{config.target_label||'选择默认目录'}}</el-button></div>
  <div class="resource-form-row"><el-switch v-model="config.auto_organize" active-text="落盘后自动整理"/><el-button type="primary" :loading="busy" @click="saveConfig">保存默认设置</el-button></div>
 </section>

 <section class="panel resource-settings">
  <div class="resource-compose-head"><div><h2>Webhook 收件</h2><p class="hint">给 Prowlarr、下载器或自建脚本推送；令牌只放在 URL 里，不需要管理员登录。</p></div><el-button :disabled="busy" @click="rotate">{{webhookToken?'重新生成':'生成令牌'}}</el-button></div>
  <el-input v-if="webhookUrl" :model-value="webhookUrl" readonly><template #append><el-button @click="copyWebhook">复制</el-button></template></el-input>
  <p v-else class="hint">还没有令牌。生成后把地址填到外部工具的 Webhook 里。</p>
 </section>

 <section class="panel resource-settings">
  <div class="resource-compose-head"><div><h2>RSS 订阅</h2><p class="hint">支持 RSS 2.0 / Atom；标题和正文里的 115 分享、磁力、ED2K、直链会进入收件箱。</p></div><div class="button-row"><el-tag type="success">正常 {{feedHealth.ok}}</el-tag><el-tag v-if="feedHealth.bad" type="warning">异常 {{feedHealth.bad}}</el-tag></div></div>
  <el-form label-position="top" class="resource-feed-form">
   <div class="resource-feed-grid"><el-form-item label="名称"><el-input v-model="feed.name" placeholder="例如：Prowlarr 电影"/></el-form-item><el-form-item label="RSS 地址"><el-input v-model="feed.url" placeholder="https://.../rss"/></el-form-item></div>
   <div class="resource-feed-grid"><el-form-item label="间隔（秒）"><el-input-number v-model="feed.interval" :min="60" :max="86400" :step="60"/></el-form-item><el-form-item label="落点"><el-button @click="openPicker('feed')">{{feed.target_cid?'目录 '+feed.target_cid:'跟随默认目录'}}</el-button></el-form-item><el-form-item label="包含关键词"><el-input v-model="feed.include" placeholder="留空不筛选"/></el-form-item><el-form-item label="排除关键词"><el-input v-model="feed.exclude" placeholder="留空不排除"/></el-form-item></div>
   <div class="resource-form-row"><el-switch v-model="feed.enabled" active-text="启用"/><el-switch v-model="feed.auto_organize" active-text="落盘后整理"/><el-button type="primary" :loading="busy" :disabled="!feed.url.trim()" @click="saveFeed">{{editing?'保存修改':'添加 RSS'}}</el-button><el-button v-if="editing" @click="cancelFeed">取消编辑</el-button></div>
  </el-form>
  <el-empty v-if="!feeds.length" description="还没有 RSS 源"/>
  <div v-else class="resource-feed-list"><div v-for="row in feeds" :key="row.id" class="resource-feed-row"><div><strong>{{row.name||row.url}}</strong><p class="hint">{{row.url}} · 每 {{row.interval}} 秒 · {{row.enabled?'启用':'停用'}} · 失败 {{row.failures||0}} 次 · 上次成功 {{fmtTime(row.last_success)}} · 下次 {{fmtTime(row.next_run)}}<span v-if="row.error"> · {{row.error}}</span></p></div><div class="button-row"><el-button size="small" :disabled="busy" @click="runFeed(row)">立即拉取</el-button><el-button size="small" :disabled="busy" @click="editFeed(row)">编辑</el-button><el-button size="small" text type="danger" :disabled="busy" @click="removeFeed(row)">删除</el-button></div></div></div>
  <details class="resource-feed-import"><summary>Cloudflare 拦截？粘贴 RSS 内容导入</summary><p class="hint">浏览器能打开 RSS 后，Ctrl+A 复制全部 XML 粘贴到这里；用于 re0 这类程序端被 Cloudflare 拦截的源。</p><el-input v-model="rssXml" type="textarea" :rows="4" maxlength="2000000" placeholder="粘贴 &lt;rss&gt;…&lt;/rss&gt; 或 &lt;feed&gt;…&lt;/feed&gt;"/><div class="button-row"><el-button :loading="importing" :disabled="!rssXml.trim()" @click="importRssXml">导入粘贴的 RSS</el-button></div></details>
 </section>

 <section class="panel resource-settings">
  <div class="resource-compose-head"><div><h2>频道监控</h2><p class="hint">Telegram 频道仍由频道监控处理；这里只显示健康状态，避免再建一套来源。</p></div><el-tag type="info">{{channels.length}} 个频道</el-tag></div>
  <el-empty v-if="!channels.length" description="还没有频道监控配置"/>
  <div v-else class="resource-feed-list"><div v-for="row in channels" :key="row.name" class="resource-feed-row"><div><strong>{{row.title||row.name}}</strong><p class="hint">状态 {{row.state||'new'}} · 最近检查 {{fmtTime(row.checked)}} · 失败 {{row.failures||0}} 次 · 上次成功 {{fmtTime(row.last_success)}} · 已处理 {{row.posts||0}} 条<span v-if="row.error"> · {{row.error}}</span></p></div><el-tag :type="row.error?'danger':'success'">{{row.error?'异常':'正常'}}</el-tag></div></div>
 </section>
</el-tab-pane>
</el-tabs>
<DirectoryPicker v-if="picker" :api="api" @select="picked" @close="picker=''"/>
</template>


<style scoped>
.resource-alert{margin-bottom:14px}
.resource-embedded-hint{margin:0 0 12px}
.embedded-tabs :deep(.el-tabs__header){display:none}
.embedded-tabs :deep(.el-tabs__content){padding-top:0}
.resource-compose,.resource-inbox,.resource-settings{margin-bottom:16px}
.resource-compose-head,.resource-toolbar{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.resource-compose-head h2{font-size:16px;margin:0}
.resource-form-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px}
.resource-form-row .el-input{flex:1 1 240px;min-width:0}
.resource-rows{display:grid;gap:8px}
.resource-row{display:flex;gap:12px;align-items:flex-start;justify-content:space-between;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--surface-soft)}
.resource-row-main{min-width:0;flex:1}
.resource-row-title{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.resource-row-title strong{font-size:14px;overflow-wrap:anywhere}
.resource-text{margin:6px 0;color:var(--text);font-size:12px;line-height:1.55;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.resource-row-actions{display:flex;gap:4px;flex-wrap:wrap;flex:none}
.resource-settings .el-input{max-width:100%}
.resource-feed-form{margin-top:8px}
.resource-feed-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 14px}
.resource-feed-list{display:grid;gap:8px;margin-top:12px}
.resource-feed-import{margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
.resource-feed-import summary{cursor:pointer;color:var(--accent);font-size:13px;font-weight:600}
.resource-feed-import .el-input{margin:10px 0}
.resource-feed-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--surface-soft)}
.resource-feed-row strong,.resource-feed-row p{overflow-wrap:anywhere}
@media(max-width:760px){.resource-row{flex-direction:column}.resource-row-actions{width:100%}.resource-feed-grid{grid-template-columns:1fr}.resource-feed-row{flex-direction:column;align-items:flex-start}.resource-compose-head,.resource-toolbar{align-items:stretch}}
</style>
