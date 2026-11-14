<script setup lang="ts">
import {computed,ref,onMounted,onUnmounted} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import {useRoute} from 'vue-router'
import CloudTreePicker from './CloudTreePicker.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const route=useRoute()
const config=ref<any>({enabled:false,notify:true,schedule_mode:'interval',interval:300,cron:'*/10 * * * *',channels:[]})
// 界面按分钟填写，接口仍按秒保存：300 秒显示为 5 分钟，谁也看不懂 300。
const intervalMinutes=computed({
  get:()=>Math.max(1,Math.round((Number(config.value.interval)||60)/60)),
  set:(value:number)=>{config.value.interval=Math.max(1,Math.round(Number(value)||1))*60}
})
const sources=ref<any[]>([]),organizeSources=ref<string[]>([])
const draft=ref(''),busy=ref(false),error=ref(''),picker=ref(''),routeIndex=ref(-1)
const baseline=ref(''),dirty=computed(()=>!!baseline.value&&JSON.stringify(config.value)!==baseline.value)

async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function absorb(data:any){config.value=data.config;sources.value=data.sources;organizeSources.value=data.organize_sources||[]
 config.value.channels=(config.value.channels||[]).map((c:any)=>{
  // 老配置是单个 mode + target_cid，读入时迁到「分享 / 离线」两路，保存后即固化。
  const mode=c.mode||'strm'
  return {...c,
   share_mode:(c.share_mode==='strm'||(!c.share_mode&&mode==='strm'))?'strm':'transfer',
   history_pages:c.history_pages??1,
   share_cid:c.share_cid??(mode==='transfer'?(c.target_cid||''):''),
   share_label:c.share_label??(mode==='transfer'?(c.target_label||''):''),
   offline_cid:c.offline_cid??(mode==='offline'?(c.target_cid||''):''),
   offline_label:c.offline_label??(mode==='offline'?(c.target_label||''):''),
   routes:Array.isArray(c.routes)?c.routes:[]}});baseline.value=JSON.stringify(config.value)}
async function load(){await run(async()=>absorb(await props.api('/channels')))}
function normalize(value:string){return value.trim().replace(/^https?:\/\/(www\.)?t(elegram)?\.me\//i,'').replace(/^s\//i,'').replace(/^@/,'').split('?')[0].replace(/\/+$/,'')}
function addChannel(){const name=normalize(draft.value);if(!name){ElMessage.warning('填一个公开频道用户名，例如 @channel');return}
 if(config.value.channels.some((c:any)=>c.name===name)){ElMessage.warning('这个频道已经在列表里了');return}
 config.value.channels.push({name,enabled:true,source:'preview',share_mode:'strm',organize_after:false,share_cid:'',share_label:'',offline_cid:'',offline_label:'',include:'',exclude:'',routes:[]});draft.value=''}
function removeChannel(name:string){config.value.channels=config.value.channels.filter((c:any)=>c.name!==name)}
const renameOpen=ref(false),renameFrom=ref(''),renameTo=ref('')
function startRename(c:any){
  if(dirty.value){ElMessage.warning('先点「保存修改」，再改名字');return}
  renameFrom.value=c.name;renameTo.value=c.name;renameOpen.value=true
}
async function confirmRename(){
  const from=renameFrom.value,to=renameTo.value.trim()
  if(!to||to===from){renameOpen.value=false;return}
  await run(async()=>{
    await props.api('/channels/rename','POST',{old:from,new:to})
    renameOpen.value=false
    absorb(await props.api('/channels'))
    ElMessage.success('已改名：落点和规则保留，跟进位置也搬过去了')
  })
}
const pickKind=ref('share')
function pick(name:string,index:number|string=-1,kind='share'){picker.value=name;routeIndex.value=Number(index);pickKind.value=kind}
function toggleOffline(c:any,kind:string,on:boolean){if(on){if(!c[kind+'_cid'])pick(c.name,-1,kind)}else{c[kind+'_cid']='';c[kind+'_label']=''}}
function picked(folder:any){
 const c=config.value.channels.find((x:any)=>x.name===picker.value)
 if(c){
  const route=c.routes[routeIndex.value]
  if(routeIndex.value>=0&&route){route.target_cid=folder.id;route.target_label=folder.name}
  else if(pickKind.value==='magnet'||pickKind.value==='ed2k'){c[pickKind.value+'_cid']=folder.id;c[pickKind.value+'_label']=folder.name}
  else{c.share_cid=folder.id;c.share_label=folder.name}
 }
 picker.value='';routeIndex.value=-1
}
function addRoute(c:any){c.routes.push({keywords:'',target_cid:'',target_label:''})}
function dropRoute(c:any,index:number|string){c.routes.splice(Number(index),1)}
function stateOf(name:string){return sources.value.find((s:any)=>s.name===name)||{}}
function stateLabel(row:any){return ({new:'未跟进',ready:'正常',backoff:'限流退避',error:'读取失败'} as any)[row.state]||row.state||'未跟进'}
function notSource(cid:string){return !!cid&&!organizeSources.value.includes(cid)}
async function save(){const missing=config.value.channels.filter((c:any)=>c.share_mode==='transfer'&&!c.share_cid&&!c.magnet_cid&&!c.ed2k_cid);if(missing.length){ElMessage.warning('「'+missing[0].name+'」还没选目录：分享转存、磁力、ED2K 至少配置一个');return}
 const payload=JSON.parse(JSON.stringify(config.value))
 payload.channels=payload.channels.map((c:any)=>{const rest={...c};delete rest.mode;delete rest.target_cid;delete rest.target_label;return rest})
 await run(async()=>{absorb(await props.api('/channels','PUT',payload));ElMessage.success('频道监控已保存')})}
async function check(){await run(async()=>{const data=await props.api('/channels/check','POST');absorb(data);ElMessage.success('已检查 '+data.checked+' 个频道，新入队 '+data.queued+' 个')})}
const tg=ref<any>({logged_in:false,phone:'',api_id:''}),tgOpen=ref(false),tgStep=ref('form'),tgBusy=ref(false)
const tgForm=ref({api_id:'',api_hash:'',phone:'',code:'',password:''}),tgChannels=ref<any[]>([])
async function loadTelegram(){try{const r=await props.api('/telegram/session');tg.value=r.session}catch{}}
async function loadTelegramChannels(){
  tgBusy.value=true
  try{
    const r:any=await props.api('/telegram/session/dialogs')
    tgChannels.value=r.items||[]
    if(tgChannels.value.length)ElMessage.success('读到 '+tgChannels.value.length+' 个会话')
    else ElMessage.warning('会话里没有频道、群组或机器人，先在 Telegram 里加入一个再读')
  }catch(e){
    tgChannels.value=[]
    ElMessage.error((e as Error).message||'读取频道失败')
  }finally{tgBusy.value=false}
}
async function tgSendCode(){tgBusy.value=true;try{await props.api('/telegram/session/code','POST',{api_id:tgForm.value.api_id,api_hash:tgForm.value.api_hash,phone:tgForm.value.phone});tgStep.value='code';ElMessage.success('验证码已发送')}catch(e){(ElMessage as any).error((e as Error).message)}finally{tgBusy.value=false}}
async function tgVerify(){tgBusy.value=true;try{const r=await props.api('/telegram/session/verify','POST',{code:tgForm.value.code});if(r.need_password){tgStep.value='password';ElMessage.warning('需要二步验证密码')}else{tgOpen.value=false;tgStep.value='form';await loadTelegram();await loadTelegramChannels();ElMessage.success('已登录')}}catch(e){(ElMessage as any).error((e as Error).message)}finally{tgBusy.value=false}}
async function tgPassword(){tgBusy.value=true;try{await props.api('/telegram/session/password','POST',{password:tgForm.value.password});tgOpen.value=false;tgStep.value='form';await loadTelegram();await loadTelegramChannels();ElMessage.success('已登录')}catch(e){(ElMessage as any).error((e as Error).message)}finally{tgBusy.value=false}}
async function tgLogout(){try{await props.api('/telegram/session/logout','POST');tg.value={logged_in:false};tgChannels.value=[];ElMessage.success('已退出')}catch(e){(ElMessage as any).error((e as Error).message)}}
const tgQuery=ref('')
const tgFiltered=computed(()=>{const q=tgQuery.value.trim().toLowerCase()
  if(!q)return tgChannels.value
  return tgChannels.value.filter((ch:any)=>String(ch.title||'').toLowerCase().includes(q)||String(ch.id).includes(q))})
function addFromTelegram(ch:any){
  const name=ch.username||ch.id
  if(config.value.channels.some((c:any)=>c.name===name)){ElMessage.warning('这个频道已经在列表里了');return}
  config.value.channels.push({name,enabled:true,source:'session',share_mode:'strm',organize_after:false,
    share_cid:'',share_label:'',offline_cid:'',offline_label:'',include:'',exclude:'',routes:[]})
  ElMessage.success('已添加，记得点「保存修改」')
}
function added(ch:any){const name=ch.username||ch.id;return config.value.channels.some((c:any)=>c.name===name)}
function kindLabel(ch:any){return ch.kind==='group'?'群组':ch.kind==='bot'?'机器人':(ch.private?'私密频道':'公开频道')}
async function canLeave(){if(!dirty.value&&!draft.value.trim())return true;try{await ElMessageBox.confirm('频道设置或输入内容尚未保存，刷新页面可能丢失。','离开频道监控？',{confirmButtonText:'离开',cancelButtonText:'继续编辑'});return true}catch{return false}}
function beforeUnload(e:BeforeUnloadEvent){if(route.name==='channels'&&(dirty.value||draft.value.trim())){e.preventDefault();e.returnValue=''}}
defineExpose({canLeave})
onMounted(()=>{window.addEventListener('beforeunload',beforeUnload);loadTelegram();load()})
onUnmounted(()=>window.removeEventListener('beforeunload',beforeUnload))
</script>
<template>
<div class="page-title channel-page-head"><div><h1>频道监控</h1><p class="muted">管理公开频道、保存目录与分流规则。</p></div><div class="channel-head-actions"><span role="status">{{!baseline?'正在读取设置':dirty?'有未保存修改':'设置已保存'}}</span><el-button type="primary" :loading="busy" :disabled="!dirty" @click="save">保存修改</el-button></div></div>
<el-alert v-if="error" :title="error" type="error" show-icon style="margin-bottom:14px"/>
<section class="panel agent-session"><div class="section-title"><h2>Telegram 用户会话</h2>
<el-tag size="small" :type="tg.logged_in?'success':'info'">{{tg.logged_in?('已登录 '+tg.phone):'未登录'}}</el-tag>
<el-button v-if="!tg.logged_in" size="small" :disabled="busy" @click="tgOpen=true">登录</el-button>
<el-button v-else size="small" :disabled="busy" @click="tgLogout">退出</el-button></div>
<p class="hint">私密频道需要用户会话读取；只读你已加入频道的消息，不发送、不删除。</p>
<div v-if="tg.logged_in" class="button-row"><el-button :loading="tgBusy" @click="loadTelegramChannels">读取频道 / 群组 / 机器人</el-button></div>
<div v-if="tgChannels.length" class="tg-tools">
 <el-input v-model="tgQuery" size="small" clearable placeholder="搜索频道名或 id" style="max-width:260px"/>
 <span class="hint">{{tgFiltered.length}} / {{tgChannels.length}} 个</span>
</div>
<div v-if="tgChannels.length" class="tg-list">
 <div v-for="ch in tgFiltered" :key="ch.id" class="tg-item">
  <div class="tg-item-main"><strong :title="ch.title||ch.id">{{ch.title||ch.id}}</strong><span class="tg-meta">{{kindLabel(ch)}} · {{ch.id}}</span></div>
  <el-button size="small" :disabled="busy||added(ch)" @click="addFromTelegram(ch)">{{added(ch)?'已添加':'添加'}}</el-button>
 </div>
</div>
<p v-if="tg.logged_in&&!tgChannels.length" class="hint">点「读取频道 / 群组 / 机器人」，再从列表里挑要监控的。机器人的链接如果在按钮里也能读到。</p>
</section>
<el-dialog v-model="tgOpen" title="登录 Telegram 用户会话" width="440px"><el-form label-position="top">
<template v-if="tgStep==='form'">
<el-form-item label="api_id"><el-input v-model="tgForm.api_id" placeholder="my.telegram.org → API development tools"/></el-form-item>
<el-form-item label="api_hash"><el-input v-model="tgForm.api_hash"/></el-form-item>
<el-form-item label="手机号（带国家码）"><el-input v-model="tgForm.phone" placeholder="+8613800000000"/></el-form-item>
<div class="button-row"><el-button type="primary" :loading="tgBusy" @click="tgSendCode">发送验证码</el-button></div>
</template>
<template v-else-if="tgStep==='code'">
<el-form-item label="验证码"><el-input v-model="tgForm.code"/></el-form-item>
<div class="button-row"><el-button type="primary" :loading="tgBusy" @click="tgVerify">验证并登录</el-button><el-button @click="tgStep='form'">重新发送</el-button></div>
</template>
<template v-else>
<el-form-item label="二步验证密码"><el-input v-model="tgForm.password" type="password" show-password/></el-form-item>
<div class="button-row"><el-button type="primary" :loading="tgBusy" @click="tgPassword">提交密码</el-button></div>
</template>
</el-form></el-dialog>

<section class="panel"><div class="section-title"><h2>轮询与通知</h2><el-tag :type="dirty?'warning':config.enabled?'success':'info'">{{dirty?'有未保存修改':config.enabled?'已启用':'已停用'}}</el-tag></div>
 <el-form label-position="top" :disabled="busy">
  <el-form-item label="启用频道监控"><el-switch v-model="config.enabled"/></el-form-item>
  <el-form-item label="轮询方式"><el-radio-group v-model="config.schedule_mode"><el-radio-button value="interval">固定间隔</el-radio-button><el-radio-button value="cron">cron 表达式</el-radio-button></el-radio-group></el-form-item>
  <el-form-item v-if="config.schedule_mode==='interval'" label="间隔（分钟）"><div style="display:flex;align-items:center;gap:8px"><el-input-number v-model="intervalMinutes" :min="1" :max="1440" :step="1"/><span class="hint" style="margin:0">最小 1 分钟</span></div></el-form-item>
  <el-form-item v-else label="cron（5 段，按服务器时区）"><el-input v-model="config.cron" placeholder="*/10 * * * *"/></el-form-item>
  <el-form-item label="抓到新分享时推送通知"><el-switch v-model="config.notify"/></el-form-item>
 </el-form>
 <div class="button-row"><el-button type="primary" :loading="busy" @click="save">保存设置</el-button><el-button :loading="busy" :disabled="dirty" @click="check">立即检查一轮</el-button><span v-if="dirty" class="hint">请先保存修改，再检查频道。</span></div>
 <p class="hint">公开预览页只支持公开频道；使用已登录的 Telegram 用户会话可读取已加入的私密频道或群组。首次检查按各频道设置的回溯页数读取，之后跟进新帖。</p>
</section>


<section class="panel"><div class="section-title"><h2>频道与规则</h2><span class="muted">{{config.channels.length}} 个</span></div>
 <div class="button-row" style="margin-bottom:6px"><el-input v-model="draft" aria-label="公开频道用户名或链接" placeholder="@channel_name 或 t.me/channel_name" style="max-width:320px" :disabled="busy" @keyup.enter="addChannel"/><el-button :disabled="busy" @click="addChannel">添加频道</el-button></div>
 <p class="hint" v-if="!config.channels.length">还没添加频道。添加后保存，再点「立即检查一轮」验证能不能读到。</p>

 <div v-for="c in config.channels" :key="c.name" class="channel-card">
  <div class="channel-head">
   <strong>{{c.name}}</strong>
   <el-button text size="small" :disabled="busy" @click="startRename(c)">改名</el-button>
   <el-tag size="small" :type="c.enabled?'success':'info'">{{c.enabled?'启用':'停用'}}</el-tag>
   <el-tag size="small" :type="stateOf(c.name).state==='error'?'danger':stateOf(c.name).state==='backoff'?'warning':'success'">{{stateLabel(stateOf(c.name))}}</el-tag>
   <el-button text type="danger" style="margin-left:auto" :disabled="busy" @click="removeChannel(c.name)">移除</el-button>
  </div>
  <p class="hint channel-meta">{{stateOf(c.name).title||'—'}} · 跟进到 {{stateOf(c.name).last_message_id||'—'}} · 已收 {{stateOf(c.name).posts||0}} 个分享</p>
  <p class="hint channel-meta" v-if="stateOf(c.name).error">{{stateOf(c.name).error}}</p>

  <div class="channel-section">
   <div class="organize-option"><label>处理这个频道</label><el-switch v-model="c.enabled" :disabled="busy"/></div>
   <div class="organize-option"><label>读取来源</label>
    <el-select v-model="c.source" :disabled="busy" style="width:240px">
     <el-option label="公开预览页（无需登录）" value="preview"/>
     <el-option label="用户会话（可读私密频道）" value="session"/>
    </el-select>
   </div>
   <div class="organize-option"><label>首次回溯</label>
    <el-input-number v-model="c.history_pages" :disabled="busy" :min="1" :max="20" :step="1" :precision="0" aria-label="首次回溯页数"/><span class="hint">页，每页约 20 条</span>
   </div>
   <div class="organize-option"><label>分享链接</label>
    <el-select v-model="c.share_mode" :disabled="busy" style="width:240px">
     <el-option label="115 分享转存到网盘" value="transfer"/>
     <el-option label="分享 STRM 落盘（免转存）" value="strm"/>
    </el-select>
   </div>
  </div>

  <div class="channel-section" v-if="c.share_mode==='transfer'">
   <div class="directory-block">
    <div class="directory-block-heading"><div><strong>转存目录</strong><p>分享链接默认保存到这里</p></div></div>
    <div class="directory-pick-row">
     <div class="directory-path" :class="{empty:!c.share_cid}"><span>115</span><strong>{{c.share_label||c.share_cid||'尚未选择目录'}}</strong></div>
     <div class="button-row"><el-button :disabled="busy" @click="pick(c.name,-1,'share')">{{c.share_cid?'更换目录':'选择目录'}}</el-button></div>
    </div>
    <p v-if="notSource(c.share_cid)" class="directory-status warning">未直接列为待整理目录，请核对整理范围</p>
    <p v-else-if="c.share_cid" class="directory-status">已列为待整理目录</p>
   </div>
  </div>
  <div v-if="c.share_mode==='transfer'" class="channel-section">
   <div v-for="kind in ['magnet','ed2k']" :key="kind" class="directory-block">
    <div class="directory-block-heading"><div><strong>{{kind==='magnet'?'磁力':'ED2K'}}离线目录</strong><p>留空则不提交该类型的离线链接</p></div></div>
    <div class="directory-pick-row">
     <div class="directory-path" :class="{empty:!c[kind+'_cid']}"><span>115</span><strong>{{c[kind+'_label']||c[kind+'_cid']||'尚未选择目录'}}</strong></div>
     <div class="button-row"><el-button :disabled="busy" @click="pick(c.name,-1,kind)">{{c[kind+'_cid']?'更换目录':'选择目录'}}</el-button><el-button v-if="c[kind+'_cid']" text :disabled="busy" @click="toggleOffline(c,kind,false)">清除</el-button></div>
    </div>
   </div>
  </div>
  <div v-if="c.share_mode==='transfer'" class="channel-section">
   <div class="routing-block">
    <div class="routing-heading"><div><strong>关键词分流</strong><p>帖子命中规则时，分享转存及磁力 / ED2K 使用该目录。</p></div><el-button :disabled="busy||c.routes.length>=20" @click="addRoute(c)">添加分流</el-button></div>
    <div v-for="(r,ri) in c.routes" :key="ri" class="route-card">
     <div class="route-header"><strong>分流 {{Number(ri)+1}}</strong><el-button text type="danger" :disabled="busy" @click="dropRoute(c,ri)">删除</el-button></div>
     <label :for="`channel-route-${c.name}-${ri}`">匹配关键词</label>
     <el-input :id="`channel-route-${c.name}-${ri}`" v-model="r.keywords" placeholder="用逗号分隔，例如 国配,国语" :disabled="busy"/>
     <div class="route-destination"><div class="route-path" :class="{empty:!r.target_cid}"><span>115</span><strong>{{r.target_label||r.target_cid||'尚未选择目录'}}</strong></div><el-button :disabled="busy" @click="pick(c.name,ri)">{{r.target_cid?'更换目录':'选择目录'}}</el-button></div>
    </div>
    <p v-if="!c.routes.length" class="route-empty">尚未设置分流，按各类型的默认目录处理。</p>
   <div class="organize-option"><label>转存或离线完成后立即整理</label>
    <el-switch :model-value="!!c.organize_after" :disabled="busy" aria-label="落盘后立即整理"
     @update:model-value="(v:boolean)=>c.organize_after=v"/>
   </div>
   <p class="hint">控制分享转存和磁力 / ED2K 离线；关闭时由「影视整理」手动或定时扫描处理。分享 STRM 的自动整理在「分享 STRM → 分享监控」设置。</p>
   </div>
  </div>
  <p v-else class="hint">免转存模式只处理 115 分享链接；磁力和 ED2K 不会生成分享 STRM，也不会提交离线下载。</p>


  <div class="channel-section">
   <div class="organize-option"><label>只收匹配的帖子（正则，留空不限）</label><el-input v-model="c.include" :disabled="busy" placeholder="例如 2160p|4K|中字"/></div>
   <div class="organize-option"><label>跳过匹配的帖子（正则）</label><el-input v-model="c.exclude" :disabled="busy" placeholder="例如 预告|求片|失效"/></div>
  </div>
 </div>
 <div class="button-row" v-if="config.channels.length" style="margin-top:14px"><el-button type="primary" :loading="busy" @click="save">保存频道规则</el-button></div>
</section>
<el-dialog v-model="renameOpen" title="改频道名字" width="440px">
 <p class="hint" style="margin:0 0 10px">频道从公开转私密后用户名会失效（预览页变 302），这时把用户名换成数字 ID，例如 <code>-1004419698318</code>。落点、正则、分流规则全部保留，跟进位置也一起搬过去，不会重扫旧帖。</p>
 <el-form label-position="top">
  <el-form-item label="当前"><el-input :model-value="renameFrom" disabled/></el-form-item>
  <el-form-item label="改成"><el-input v-model="renameTo" placeholder="@channel_name 或 -100xxxxxxxxxx" @keyup.enter="confirmRename"/></el-form-item>
 </el-form>
 <template #footer><div class="button-row"><el-button @click="renameOpen=false">取消</el-button><el-button type="primary" :loading="busy" @click="confirmRename">改</el-button></div></template>
</el-dialog>
<CloudTreePicker v-if="picker" :api="api" @select="picked" @close="picker=''"/>
</template>
<style scoped>
.channel-card{border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-top:12px;background:var(--surface)}
.channel-page-head{position:sticky;top:60px;z-index:20;padding:15px 18px;margin:0 0 16px;border:1px solid var(--line);border-radius:10px;background:var(--surface);box-shadow:var(--panel-shadow)}.channel-page-head h1{font-size:23px}.channel-head-actions{display:flex;align-items:center;gap:12px;flex:none}.channel-head-actions span{font-size:12px;color:var(--muted)}
.channel-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.channel-meta{margin:6px 0 0}
.channel-section{margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)}
.channel-dir{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
</style>

<style scoped>

.channel-card{padding:18px;margin-top:14px;border-radius:10px;background:var(--surface-soft)}.channel-head strong{font-size:14px;color:var(--text)}.channel-meta{line-height:1.65}.channel-section{border-top-style:solid;margin-top:15px;padding-top:14px}.channel-section .organize-option{display:flex;align-items:center;justify-content:space-between;gap:16px;min-width:0;padding:9px 0}.channel-section .organize-option>label{font-size:13px;line-height:1.5;color:var(--text)}.channel-section .organize-option :deep(.el-input){max-width:440px;min-width:0}.channel-section .organize-option :deep(.el-select){max-width:100%}.channel-dir :deep(.el-button){max-width:100%;white-space:normal;height:auto;min-height:32px}.channel-dir :deep(.el-input){min-width:0}.channel-card :deep(.el-tag){max-width:100%;white-space:normal;height:auto;line-height:1.5}
@media(max-width:760px){.channel-card{padding:14px}.channel-section .organize-option{flex-wrap:wrap;align-items:center;gap:8px}.channel-section .organize-option>label{flex:1 1 55%}.channel-section .organize-option :deep(.el-input),.channel-section .organize-option :deep(.el-select){flex:1 1 100%;width:100%!important;max-width:none}.channel-dir{gap:7px}.channel-dir :deep(.el-input){width:100%!important}.channel-head strong{overflow-wrap:anywhere}}
@media(max-width:760px){.channel-page-head{top:60px;padding:11px 13px;align-items:center}.channel-page-head h1{font-size:19px}.channel-page-head p{font-size:11px}.channel-head-actions{gap:6px;flex-direction:column;align-items:flex-end}.channel-head-actions span{font-size:10px}.channel-head-actions :deep(.el-button){min-height:34px;padding:7px 10px}}
.directory-block,.routing-block{min-width:0}.directory-block-heading,.routing-heading,.route-header,.route-destination{display:flex;align-items:center;justify-content:space-between;gap:12px}.directory-block-heading>div,.routing-heading>div,.route-destination>div{min-width:0;flex:1}.directory-block-heading strong,.routing-heading strong,.route-header strong{font-size:13px;color:var(--text)}.directory-block-heading p,.routing-heading p{margin:5px 0 0;font-size:12px;line-height:1.6;color:var(--muted)}.directory-block-heading :deep(.el-button),.routing-heading :deep(.el-button),.route-destination :deep(.el-button){flex:none;margin:0}.directory-path{display:flex;align-items:flex-start;gap:10px;margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:8px;background:var(--surface);min-width:0}.directory-path span{flex:none;padding:3px 6px;border-radius:5px;background:var(--el-color-primary-light-9);color:var(--accent);font-size:10px;font-weight:700}.directory-path strong,.route-destination strong{min-width:0;overflow-wrap:anywhere;color:var(--text);font-size:13px;line-height:1.55}.directory-path.empty strong{color:var(--muted);font-weight:400}.directory-status{margin:8px 0 0;font-size:12px;line-height:1.6;color:var(--el-color-success)}.directory-status.warning{color:var(--el-color-warning)}.routing-block{margin-top:20px;padding-top:18px;border-top:1px solid var(--line)}.route-card{margin-top:12px;padding:14px;border:1px solid var(--line);border-radius:9px;background:var(--surface)}.route-header{margin-bottom:9px}.route-header :deep(.el-button){margin:0}.route-card>label{display:block;margin:0 0 7px;font-size:12px;color:var(--muted)}.route-destination{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}.route-destination span,.route-destination strong{display:block}.route-destination span{font-size:11px;color:var(--muted);margin-bottom:4px}.route-empty{margin:12px 0 0;padding:12px;border:1px dashed var(--line);border-radius:8px;color:var(--muted);font-size:12px;line-height:1.6}
@media(max-width:760px){.directory-block-heading,.routing-heading{align-items:flex-start}.directory-path{padding:10px}.route-card{padding:12px}.route-destination{align-items:flex-start}.route-destination :deep(.el-button){min-height:36px}.route-destination strong{word-break:break-word}}
.offline-destinations-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.offline-destination-card{min-width:0;padding:12px;border:1px solid var(--line);border-radius:9px;background:var(--surface)}.offline-destination-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.offline-destination-head strong{font-size:13px}.offline-destination-card p{margin:9px 0;font-size:12px;line-height:1.6;color:var(--text);overflow-wrap:anywhere}.offline-destination-card p.offline-destination-empty{color:var(--muted)}.offline-destination-card :deep(.el-button){margin:0}
@media(max-width:560px){.offline-destinations-grid{grid-template-columns:1fr}}

.tg-tools{display:flex;align-items:center;gap:12px;margin:12px 0 10px;flex-wrap:wrap}
.tg-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:10px;background:var(--surface)}
.tg-item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 10px;border-radius:8px;background:var(--surface-soft)}
.tg-item-main{display:grid;gap:3px;min-width:0}
.tg-item-main strong{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tg-meta{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
@media(max-width:760px){.tg-list{grid-template-columns:1fr;max-height:none}}
.tg-tools{display:flex;align-items:center;gap:12px;margin:12px 0 10px;flex-wrap:wrap}.tg-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:10px;background:var(--surface)}.tg-item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 10px;border-radius:8px;background:var(--surface-soft)}.tg-item-main{display:grid;gap:3px;min-width:0}.tg-item-main strong{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tg-meta{font-size:11px;color:var(--muted)}@media(max-width:760px){.tg-list{grid-template-columns:1fr;max-height:none}}.directory-pick-row{display:flex;align-items:center;gap:10px;margin-top:12px}.directory-pick-row .directory-path{margin-top:0;flex:1;min-width:0}.directory-pick-row .button-row{flex:none;display:flex;gap:6px;margin:0}.route-path{display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid var(--line);border-radius:8px;background:var(--surface);min-width:0;flex:1}.route-path span{flex:none;padding:3px 6px;border-radius:5px;background:var(--el-color-primary-light-9);color:var(--accent);font-size:10px;font-weight:700}.route-path strong{min-width:0;overflow-wrap:anywhere;color:var(--text);font-size:13px;line-height:1.55}.route-path.empty strong{color:var(--muted);font-weight:400}@media(max-width:760px){.directory-pick-row{flex-direction:column;align-items:stretch}.directory-pick-row .button-row{justify-content:flex-end}}</style>
