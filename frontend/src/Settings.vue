<script setup lang="ts">
import {onMounted,ref,watch} from 'vue'
import {ElMessage} from 'element-plus'
import {useRoute} from 'vue-router'
const route=useRoute()
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>;tab?:string}>();const emit=defineEmits<{appearance:[value:any]}>()
const SIDEBAR_MENU=[
 {key:'media',label:'媒体管理',items:[
  {id:'overview',title:'媒体总览'},{id:'explore',title:'影视探索'},{id:'inventory',title:'库存管理'},
  {id:'account',title:'115 网盘'},{id:'transfer',title:'资源入库'},{id:'sharestrm',title:'分享 STRM'},
  {id:'channels',title:'频道监控'},{id:'organize',title:'影视整理'},{id:'recognize',title:'识别测试'},
  {id:'sync',title:'STRM 管理'}]},
 {key:'system',label:'运行与系统',items:[
  {id:'embyproxy',title:'Emby 反代'},{id:'tasks',title:'任务中心'},{id:'logs',title:'实时日志'},
  {id:'settings',title:'系统设置'}]}]
const sidebarOrder=ref<Record<string,string[]>>({})
function loadSidebarOrder(){try{sidebarOrder.value=JSON.parse(localStorage.getItem('sk.sidebar.order')||'{}')||{}}catch{sidebarOrder.value={}}}
function sidebarItems(group:any){
  const saved=sidebarOrder.value[group.key]||[]
  const byId=new Map<string,any>(group.items.map((i:any)=>[String(i.id),i]))
  const out=saved.map((id:string)=>byId.get(id)).filter(Boolean) as any[]
  for(const i of group.items) if(!out.includes(i)) out.push(i)
  return out
}
function moveSidebar(group:any,id:string,delta:number){
  const items=sidebarItems(group)
  const index=items.findIndex((i:any)=>i.id===id)
  const target=index+delta
  if(target<0||target>=items.length) return
  const ids=items.map((i:any)=>i.id)
  const swap=ids[index]; ids[index]=ids[target]; ids[target]=swap
  sidebarOrder.value={...sidebarOrder.value,[group.key]:ids}
  localStorage.setItem('sk.sidebar.order',JSON.stringify(sidebarOrder.value))
  window.dispatchEvent(new CustomEvent('sk-sidebar-order'))
}
function resetSidebarOrder(){
  localStorage.removeItem('sk.sidebar.order')
  sidebarOrder.value={}
  window.dispatchEvent(new CustomEvent('sk-sidebar-order'))
}
const connections=ref({tmdb_key:'',emby_url:'',emby_key:'',playback_base:''}),mask=ref<any>({}),mediaBusy=ref(false),mediaMsg=ref(''),mediaInfo=ref<any>({enabled:true,extract:true,cron_enabled:false,cron:'0 4 * * *',timezone:'Asia/Shanghai'}),tg=ref<any>({telegram_enabled:false,bot_token:'',chat_id:'',webhook_enabled:false,webhook_url:'',webhook_secret:'',categories:{}}),tgMask=ref<any>({}),appearance=ref<any>({theme:'system',background_url:'',background_opacity:.12,strm_container_path:'/strm',emby_strm_path:'/strimkeep'}),network=ref<any>({enabled:false,proxy_emby:false,proxy_url:''}),networkMask=ref<any>({}),throttle=ref<any>({read_ms:350,write_ms:3000,burst:15,pause_ms:30000}),busy=ref(false),error=ref(''),tab=ref(['resource','telegram','emby'].includes(String(route.query.section))?String(route.query.section):props.tab||'services'),checks=ref<Record<string,string>>({}),coverBusy=ref(false),coverMsg=ref(''),transferBot=ref<any>({bot_enabled:false,allowed_user_id:''}),transferBotAll=ref<any>(null),botState=ref<any>({state:'disabled'}),botCmds=ref<any[]>([])
const coverStatus=ref<any>(null),coverStatusError=ref('')
async function loadCoverStatus(){try{coverStatus.value=await props.api('/cover/status');coverStatusError.value=''}catch(e){coverStatusError.value=(e as Error).message}}
async function runCover(){coverBusy.value=true;coverMsg.value='';try{await props.api('/cover/run','POST');coverMsg.value='封面补齐已开始，可查看状态或实时日志。';await loadCoverStatus()}catch(e){coverMsg.value=(e as Error).message}finally{coverBusy.value=false}}
watch(()=>[connections.value.emby_url,connections.value.emby_key],()=>{delete checks.value.emby})
const loaded=ref({services:false,telegram:false,appearance:false,network:false,media:false,resource:false,commands:false})
const mediaStatus=ref<any>(null),mediaFailures=ref<any>(null),mediaStatusBusy=ref(false),mediaStatusError=ref('')
async function loadMediaStatus(){if(mediaStatusBusy.value)return;mediaStatusBusy.value=true;mediaStatusError.value='';try{const [status,failures]=await Promise.all([props.api('/emby/media-info/status'),props.api('/emby/media-info/failures?limit=200')]);mediaStatus.value=status;mediaFailures.value=failures}catch(e){mediaStatusError.value=(e as Error).message}finally{mediaStatusBusy.value=false}}
function requireLoaded(section:keyof typeof loaded.value){if(!loaded.value[section])throw new Error('该部分设置未成功读取，请刷新页面后重试。')}
const categories=[['media','入库海报与媒体信息'],['cloud','115 签到与回收站'],['metadata','STRM 通知中的 NFO 和媒体信息统计'],['transfer','转存结果'],['strm_summary','STRM 同步汇总'],['strm_files','STRM 文件明细'],['errors','失败与登录异常'],['emby','Emby 通知未确认'],['general','其他通知（包括深度删除）']]
async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function saveConnections(){await run(async()=>{requireLoaded('services');const c:any={emby_url:connections.value.emby_url,playback_base:connections.value.playback_base};if(connections.value.tmdb_key)c.tmdb_key=connections.value.tmdb_key;if(connections.value.emby_key)c.emby_key=connections.value.emby_key;mask.value=await props.api('/settings','PUT',c);connections.value.tmdb_key='';connections.value.emby_key='';ElMessage.success('服务连接已保存')})}
async function saveMediaInfo(){await run(async()=>{requireLoaded('media');mediaInfo.value={...mediaInfo.value,...await props.api('/emby/media-info/config','PUT',{enabled:!!mediaInfo.value.enabled,extract:!!mediaInfo.value.extract,cron_enabled:!!mediaInfo.value.cron_enabled,cron:mediaInfo.value.cron})};ElMessage.success('媒体信息设置已保存')})}
async function injectMedia(){if(mediaBusy.value)return;mediaBusy.value=true;mediaMsg.value='';try{const r:any=await props.api('/emby/media-info/inject','POST');const bad=(r.failures||[]).slice(0,3).map((f:any)=>String(f.path||'').split('/').pop()+'：'+f.reason);const tail=(r.failed||bad.length)?('，失败 '+(r.failed||bad.length)+' 条（'+bad.join('、')+(r.failed>bad.length?' 等':'')+'）'):'';mediaMsg.value=r.injected?('已写入 '+r.injected+' 条（'+r.streams+' 条流）'+tail):('没有需要补的条目（候选 '+r.candidates+' 条）'+tail);ElMessage.success(mediaMsg.value)}catch(e){mediaMsg.value=(e as Error).message;ElMessage.error(mediaMsg.value)}finally{mediaBusy.value=false}}
async function check(kind:string){await run(async()=>{const value=kind==='emby'?{emby_url:connections.value.emby_url,emby_key:connections.value.emby_key}:undefined;await props.api('/connections/'+kind+'/test','POST',value);checks.value[kind]=kind==='tmdb'?'已保存的 TMDB 配置连接正常；新填写的 Key 需保存后再检查':'填写的地址和密钥（留空时使用已保存的值）连接正常';ElMessage.success('连接正常')})}
async function testWebhook(){await run(async()=>{const r:any=await props.api('/notifications/webhook/test','POST',{});if(r.state==='sent')ElMessage.success(r.message);else ElMessage.error(r.message)})}
async function saveTg(){await run(async()=>{requireLoaded('telegram');tgMask.value=await props.api('/notifications/config','PUT',tg.value);tg.value.bot_token='';tg.value.chat_id='';tg.value.webhook_secret='';ElMessage.success('通知设置已保存')})}
async function loadTransferBot(){try{const r:any=await props.api('/transfer/config');transferBotAll.value=r.config;transferBot.value={bot_enabled:!!r.config.bot_enabled,allowed_user_id:r.config.allowed_user_id||''};botState.value=r.bot||{state:'disabled'}}catch(e){error.value=(e as Error).message}}
async function saveTransferBot(){await run(async()=>{const current:any=await props.api('/transfer/config');const r:any=await props.api('/transfer/config','PUT',{...current.config,bot_enabled:transferBot.value.bot_enabled,allowed_user_id:transferBot.value.allowed_user_id});transferBotAll.value=r.config;botState.value=r.bot||botState.value;ElMessage.success('接收设置已保存')})}
async function loadBotCommands(){try{botCmds.value=(await props.api('/bot/commands')).commands||[];loaded.value.commands=true}catch(e){error.value=(e as Error).message}}
function moveBotCommand(index:number,delta:number){const to=index+delta;if(to<0||to>=botCmds.value.length)return;const rows=[...botCmds.value];[rows[index],rows[to]]=[rows[to],rows[index]];botCmds.value=rows}
async function saveBotCommands(){await run(async()=>{requireLoaded('commands');const enabled:any={};for(const c of botCmds.value)enabled[c.name]=!!c.enabled;botCmds.value=(await props.api('/bot/commands','PUT',{enabled,order:botCmds.value.map(c=>c.name)})).commands||[];ElMessage.success('机器人指令设置已保存，菜单将在机器人连接后更新')})}
async function testTg(){await run(async()=>{ElMessage((await props.api('/notifications/test','POST')).message)})}
async function saveAppearance(){await run(async()=>{requireLoaded('appearance');const c=(await props.api('/console/config')).config;appearance.value=(await props.api('/console/config','PUT',{...c,theme:appearance.value.theme,background_url:appearance.value.background_url,background_opacity:appearance.value.background_opacity})).config;emit('appearance',appearance.value);ElMessage.success('外观已保存')})}
async function saveThrottle(){await run(async()=>{requireLoaded('services');mask.value=await props.api('/settings','PUT',{cloud_throttle:throttle.value});ElMessage.success('请求节奏已保存')})}
async function saveNetwork(){await run(async()=>{requireLoaded('network');networkMask.value=await props.api('/network/config','PUT',network.value);network.value.proxy_url='';ElMessage.success('网络代理已保存并生效')})}
const resource=ref({base_url:'',token:''}),resourceConfigured=ref(false)
async function loadResource(){try{const r=await props.api('/explore/resource-config');resource.value.base_url=r.base_url||'';resource.value.token='';resourceConfigured.value=!!r.token_configured;loaded.value.resource=true}catch(e){loaded.value.resource=false;error.value=(e as Error).message}}
async function saveResource(){await run(async()=>{requireLoaded('resource');const r=await props.api('/explore/resource-config','PUT',resource.value);resource.value.token='';resourceConfigured.value=!!r.token_configured;ElMessage.success('资源来源已保存')})}
async function clearResource(){await run(async()=>{requireLoaded('resource');const r=await props.api('/explore/resource-config','PUT',{base_url:resource.value.base_url,clear_token:true});resource.value.token='';resourceConfigured.value=!!r.token_configured;ElMessage.success('已清除本地令牌')})}
watch(tab,value=>{if(value==='resource'&&!loaded.value.resource)void loadResource();if(value==='telegram'){if(!transferBotAll.value)void loadTransferBot();if(!loaded.value.commands)void loadBotCommands()}})
watch(()=>route.query.section,value=>{if(value==='resource'||value==='telegram'||value==='emby')tab.value=value})
watch(()=>props.tab,value=>{if(value)tab.value=value})
onMounted(()=>{if(tab.value==='resource')void loadResource();run(async()=>{
 const results=await Promise.allSettled([props.api('/settings'),props.api('/notifications/config'),props.api('/console/config'),props.api('/network/config')])
 const failed:string[]=[]
 const [services,notifications,consoleConfig,proxy]=results
 if(services.status==='fulfilled'){const s=services.value;mask.value=s;connections.value.emby_url=s.emby_url||'';connections.value.playback_base=s.playback_base||'';if(s.cloud_throttle)throttle.value=s.cloud_throttle;loaded.value.services=true}else failed.push('服务连接')
 if(notifications.status==='fulfilled'){const n=notifications.value;tgMask.value=n;tg.value.telegram_enabled=n.telegram_enabled;tg.value.webhook_enabled=n.webhook_enabled;tg.value.webhook_url=n.webhook_url_configured?'':'';tg.value.categories=n.categories||{};loaded.value.telegram=true}else failed.push('Telegram 通知')
 if(consoleConfig.status==='fulfilled'){appearance.value=consoleConfig.value.config;loaded.value.appearance=true}else failed.push('外观')
 if(proxy.status==='fulfilled'){const p=proxy.value;networkMask.value=p;network.value.enabled=p.enabled;network.value.proxy_emby=p.proxy_emby;loaded.value.network=true}else failed.push('网络代理')
 if(failed.length)error.value=failed.join('、')+'读取失败；请刷新页面后再修改这些设置。'
 try{const m=await props.api('/emby/media-info/config');mediaInfo.value={enabled:m.enabled!==false,extract:!!m.extract,cron_enabled:!!m.cron_enabled,cron:m.cron||'0 4 * * *',timezone:m.timezone||'Asia/Shanghai'};loaded.value.media=true}catch(e){error.value+=(error.value?'；':'')+'媒体信息设置读取失败：'+(e as Error).message}
 });void loadTransferBot();void loadBotCommands()})
</script>
<template><div class="page-title"><div><h1>系统设置</h1><p class="muted">连接服务、通知分类、网络代理与工作台外观。</p></div></div><el-alert v-if="error" :title="error" type="error"/><el-tabs v-model="tab" class="settings-tabs"><el-tab-pane label="服务连接" name="services"/><el-tab-pane label="资源来源" name="resource"/><el-tab-pane label="Telegram 通知" name="telegram"/><el-tab-pane label="网络代理" name="network"/><el-tab-pane label="外观" name="appearance"/><el-tab-pane label="侧边栏" name="sidebar"/></el-tabs>
<template v-if="tab==='services'"><section class="panel settings-panel"><h2>TMDB</h2><el-form label-position="top" :disabled="busy"><el-form-item label="TMDB API Key"><el-input v-model="connections.tmdb_key" type="password" autocomplete="new-password" show-password :placeholder="mask.tmdb_key_configured?'已保存，留空保留原值':'填写 API Key'"/></el-form-item></el-form><el-button :loading="busy" @click="check('tmdb')">检查连接</el-button><span class="hint">{{checks.tmdb}}</span></section><section class="panel settings-panel"><h2>115 请求节奏</h2><el-form label-position="top" :disabled="busy"><el-form-item label="读接口最小间隔（毫秒）"><el-input-number v-model="throttle.read_ms" :min="0" :max="5000" :step="50"/></el-form-item><el-form-item label="写接口最小间隔（移动/重命名/删除，毫秒）"><el-input-number v-model="throttle.write_ms" :min="0" :max="15000" :step="500"/></el-form-item></el-form><p class="hint">后台请求按设置的读写间隔执行；115 返回限流时会自动放慢并冷却。批量整理不再按固定写入次数额外暂停。</p><el-button :loading="busy" @click="saveThrottle">保存请求节奏</el-button></section><div class="save-bar"><el-button type="primary" :loading="busy" @click="saveConnections">保存服务连接</el-button></div></template>
<section v-if="tab==='resource'" class="panel settings-panel"><h2> 资源来源</h2><p class="hint">用于影视探索检索资源；转存目标在「资源入库 → 保存目录」里单独设置（留空则跟随 Web 转存目录）。</p><el-form label-position="top" :disabled="busy"><el-form-item label=" 服务地址"><el-input v-model="resource.base_url" placeholder="http://192.0.2.10:12366"/></el-form-item><el-form-item label="OpenAPI 令牌"><el-input v-model="resource.token" type="password" show-password autocomplete="new-password" :placeholder="resourceConfigured?'已加密保存，地址不变时留空保留原值':'填写  OpenAPI 令牌'"/></el-form-item></el-form><p class="hint">更换服务地址时需要重新填写令牌。获取需要积分的资源前会单独提示确认。</p><div class="button-row"><el-button type="primary" :loading="busy" @click="saveResource">保存资源来源</el-button><el-button v-if="resourceConfigured" :disabled="busy" @click="clearResource">清除本地令牌</el-button></div></section>
<section v-if="tab==='telegram'" class="panel settings-panel"><h2>Telegram 机器人与通知</h2><el-form label-position="top" :disabled="busy"><el-form-item label="启用 Telegram 推送"><el-switch v-model="tg.telegram_enabled"/></el-form-item><el-form-item label="Bot Token"><el-input v-model="tg.bot_token" type="password" autocomplete="new-password" show-password :placeholder="tgMask.bot_token_configured?'已加密保存，留空保留原值':'填写 Bot Token'"/></el-form-item><el-form-item label="通知接收 Chat ID"><el-input v-model="tg.chat_id" autocomplete="off" :placeholder="tgMask.chat_id_configured?'已保存，留空保留原值':'用户、群组或频道 ID'"/></el-form-item></el-form><h3>通用 Webhook</h3><el-form label-position="top" :disabled="busy"><el-form-item label="启用 Webhook 推送"><el-switch v-model="tg.webhook_enabled"/></el-form-item><el-form-item label="Webhook 地址"><el-input v-model="tg.webhook_url" type="password" show-password autocomplete="new-password" :placeholder="tgMask.webhook_url_configured?'已保存，留空保留原值':'https://...'"/></el-form-item><el-form-item label="请求令牌（可选）"><el-input v-model="tg.webhook_secret" type="password" show-password autocomplete="new-password" :placeholder="tgMask.webhook_secret_configured?'已保存，留空保留原值':'写入 X-StrimKeep-Token'"/></el-form-item><div class="button-row"><el-button :disabled="busy||!tgMask.webhook_url_configured" @click="testWebhook">发送 Webhook 测试</el-button></div></el-form><h3>选择要发送的消息</h3><div class="notification-choices"><div v-for="[key,label] in categories" :key="key"><span>{{label}}</span><el-switch v-model="tg.categories[key]" :aria-label="label"/></div></div><p class="hint">定时整理未发现资源只写入日志，不推送 TG。机器人私聊的指令回执正常回复；是否接收链接、谁能操作在「资源入库」页的「Telegram 接收链接」中设置。</p><div class="button-row"><el-button type="primary" :loading="busy" @click="saveTg">保存通知设置</el-button><el-button :disabled="busy||!tgMask.bot_token_configured" @click="testTg">发送测试通知</el-button></div></section>
<section v-if="tab==='telegram'" class="panel settings-panel"><h2>Telegram 接收链接</h2><p class="hint">私聊机器人发送链接即可转存，复用上面的 Bot Token；落盘后是否立即整理在「资源入库」页的「落盘后处理」里设置。</p><el-form label-position="top" :disabled="busy"><el-form-item label="启用转存机器人"><el-switch v-model="transferBot.bot_enabled"/></el-form-item><el-form-item label="允许操作的 TG 用户数字 ID"><el-input v-model="transferBot.allowed_user_id" placeholder="仅此用户可以私聊机器人发起转存"/></el-form-item></el-form><p v-if="transferBot.bot_enabled" class="hint">{{botState.state==='ready'?'机器人已连接，可发送新链接':botState.state==='error'?botState.message:'机器人正在连接，请稍候再发链接'}}。启用前的旧消息不会自动执行。</p><div class="button-row"><el-button type="primary" :loading="busy" @click="saveTransferBot">保存接收设置</el-button></div><h3>机器人指令</h3><p class="hint">使用上下按钮调整菜单顺序；关闭的指令不会显示在菜单中，也不会执行。帮助始终显示在最前面。</p><div class="bot-command-grid"><div v-for="(c,index) in botCmds" :key="c.name" class="bot-command-row"><div class="command-label"><strong>{{c.label}}</strong><code>/{{c.name}}</code></div><div class="command-actions"><el-button size="small" :disabled="busy||index===0" :aria-label="`上移${c.label}`" @click="moveBotCommand(index,-1)">↑</el-button><el-button size="small" :disabled="busy||index===botCmds.length-1" :aria-label="`下移${c.label}`" @click="moveBotCommand(index,1)">↓</el-button><el-switch v-model="c.enabled" :aria-label="c.label"/></div></div></div><div class="button-row"><el-button :loading="busy" @click="saveBotCommands">保存指令设置</el-button></div></section>
<section v-if="tab==='sidebar'" class="panel settings-panel"><h2>侧边栏顺序</h2>
<p class="hint">只在同一分组内上下移动；保存后左侧导航和底部常用导航都会按这个顺序显示。</p>
<div v-for="group in SIDEBAR_MENU" :key="group.key" class="sidebar-order-group">
  <h3>{{ group.label }}</h3>
  <div v-for="(item, index) in sidebarItems(group)" :key="item.id" class="sidebar-order-row">
    <span class="sidebar-order-name">{{ index + 1 }}. {{ item.title }}</span>
    <span class="sidebar-order-actions">
      <el-button size="small" :disabled="index === 0" @click="moveSidebar(group, item.id, -1)">上移</el-button>
      <el-button size="small" :disabled="index === sidebarItems(group).length - 1" @click="moveSidebar(group, item.id, 1)">下移</el-button>
    </span>
  </div>
</div>
<el-button @click="resetSidebarOrder">恢复默认顺序</el-button></section>
<section v-if="tab==='network'" class="panel settings-panel"><h2>HTTP 代理</h2><el-form label-position="top" :disabled="busy"><el-form-item label="启用代理"><el-switch v-model="network.enabled"/></el-form-item><el-form-item label="代理地址"><el-input v-model="network.proxy_url" type="password" autocomplete="new-password" show-password :placeholder="networkMask.proxy_configured?'已保存，留空保留原值':'http://192.0.2.10:7890'"/></el-form-item><el-form-item label="远程 Emby 请求也通过代理"><el-switch v-model="network.proxy_emby"/></el-form-item></el-form><p class="hint">用于 TMDB、Telegram 等服务。同机 Emby 固定直连；远程 Emby 可启用代理。115 文件传输与播放取链保持独立直连。</p><el-button type="primary" :loading="busy" @click="saveNetwork">保存网络设置</el-button></section>
<section v-if="tab==='appearance'" class="panel settings-panel"><h2>工作台外观</h2><el-form label-position="top" :disabled="busy"><el-form-item label="显示模式"><el-radio-group v-model="appearance.theme" @change="emit('appearance',appearance)"><el-radio-button value="light">浅色</el-radio-button><el-radio-button value="dark">深色</el-radio-button><el-radio-button value="system">跟随系统</el-radio-button></el-radio-group></el-form-item><el-form-item label="自定义背景图片地址"><el-input v-model="appearance.background_url" clearable placeholder="https://example.com/background.jpg"/></el-form-item><el-form-item label="背景显示强度"><el-slider v-model="appearance.background_opacity" :min="0" :max=".5" :step=".01"/></el-form-item></el-form><p class="hint">图片地址留空使用默认背景。深色模式同时适配导航、表单、弹窗、目录表格与日志。</p><el-button type="primary" :loading="busy" @click="saveAppearance">保存外观</el-button></section></template>

<style scoped>
.sidebar-order-group{margin:14px 0}
.sidebar-order-group h3{font-size:13px;margin:0 0 8px;color:var(--el-text-color-secondary)}
.sidebar-order-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--el-border-color-lighter)}
.sidebar-order-name{font-size:13px}


.media-diagnostics{margin:16px 0;padding:12px;border:1px solid var(--line);border-radius:9px}.media-diagnostics summary{cursor:pointer}.media-diagnostics .file-path{overflow-wrap:anywhere;font-size:12px;color:var(--muted)}.media-failure{padding:9px 0;border-top:1px solid var(--line);font-size:13px}
.settings-tabs :deep(.el-tabs__header){margin-bottom:18px}.settings-panel h2{padding-bottom:12px;border-bottom:1px solid var(--line);margin-bottom:18px}.settings-panel :deep(.el-form-item__label){font-weight:600}.settings-panel .hint{line-height:1.75}.notification-choices>div{padding:13px 2px;gap:14px}.settings-panel :deep(.el-input__wrapper){min-height:36px}.settings-panel :deep(.el-input-number){max-width:100%}
@media(max-width:760px){.settings-tabs :deep(.el-tabs__item){padding:0 12px}.settings-panel h2{margin-bottom:14px}.notification-choices>div{padding:12px 1px}.settings-panel .button-row :deep(.el-button){margin-left:0}}
.bot-command-grid{display:grid;grid-template-columns:1fr;gap:8px;margin:12px 0}.command-label{display:grid;gap:4px;flex:1;min-width:0}.command-actions{display:flex;align-items:center;gap:6px}.command-actions :deep(.el-button){margin:0}.bot-command-row{display:flex;align-items:center;gap:10px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px}.bot-command-row code{color:var(--el-color-primary);font-size:12px}.bot-command-row span{flex:1}</style>
