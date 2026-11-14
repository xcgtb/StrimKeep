<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref,shallowRef,nextTick,watch} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import CloudLoginDialog from './CloudLoginDialog.vue'
import CloudEntryList from './CloudEntryList.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>();const emit=defineEmits<{navigate:[page:string]}>()
const qr=ref(false),cookie=ref(''),configured=ref(false),status=ref('未检查'),error=ref(''),busy=ref(false),files=ref<any[]>([]),cid=ref('0'),path=ref([{id:'0',name:'115 网盘'}]),offset=ref(0),more=ref(false),next=ref(0)
async function run(fn:()=>Promise<void>){busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function check(){await run(async()=>{try{const r=await props.api('/connections/115/test','POST');if(r&&r.connected===false){status.value='未登录';ElMessage.warning(r.message||'115 账号未登录，请扫码或填写 Cookie');return}status.value='已登录';configured.value=true;ElMessage.success('115 登录正常')}catch(e){status.value='检查失败';throw e}});await loadAccount(true)}
const account=ref<any>(null),accountBusy=ref(false)
async function loadAccount(refresh=false){accountBusy.value=true;try{account.value=await props.api('/cloud/account'+(refresh?'?refresh=1':''))}catch(e){account.value={error:(e as Error).message}}finally{accountBusy.value=false}}
const tb=(n?:number)=>n==null?'—':(n/1024**4).toFixed(2)+' TB'
const pct=(used?:number,total?:number)=>{const t=Number(total||0);return t>0?Math.min(100,Math.round(Number(used||0)/t*100)):0}
async function saved(){qr.value=false;const wasConfigured=configured.value;await check();if(wasConfigured&&configured.value)await browse('0')}
async function saveCookie(){await run(async()=>{await props.api('/settings','PUT',{cookie:cookie.value});cookie.value='';configured.value=true;status.value='已保存';ElMessage.success('Cookie 已保存')})}
const listLoading=ref(false);let browseSequence=0
async function browse(id=cid.value,name?:string,index?:number,page=0,trail?:{id:string;name:string}[]){
 const sequence=++browseSequence
 const targetPath=trail?[...trail]:index!==undefined?path.value.slice(0,index+1):name?[...path.value,{id,name}]:[...path.value]
 listLoading.value=true;error.value=''
 try{const d=await props.api(`/cloud/files?cid=${encodeURIComponent(id)}&offset=${page}&limit=100`);if(sequence!==browseSequence)return;files.value=d.items;cid.value=id;offset.value=page;more.value=d.has_more;next.value=d.next_offset??page+d.items.length;path.value=targetPath;await revealTree(sequence)}catch(e){if(sequence===browseSequence){error.value=(e as Error).message;tree.value?.setCurrentKey(cid.value)}}finally{if(sequence===browseSequence)listLoading.value=false}
}
type FolderNode={id:string;name:string;children?:FolderNode[];loaded?:boolean;more?:boolean;parent?:string;offset?:number;trail?:{id:string;name:string}[];disabled?:boolean}
const tree=ref<any>(),treeData=shallowRef<FolderNode[]>([{id:'0',name:'115 网盘',children:[{id:'placeholder:0',name:'展开读取目录',disabled:true}],trail:[{id:'0',name:'115 网盘'}]}]),expanded=new Set(['0']),treeHeight=ref(window.innerWidth<760?220:420),treeBusy=new Set<string>()
function findNode(id:string,nodes=treeData.value):FolderNode|undefined{for(const node of nodes){if(node.id===id)return node;const child=node.children&&findNode(id,node.children);if(child)return child}}
function replaceNode(id:string,patch:Partial<FolderNode>,nodes=treeData.value):FolderNode[]{return nodes.map(n=>n.id===id?{...n,...patch}:n.children?{...n,children:replaceNode(id,patch,n.children)}:n)}
async function expandFolder(node:FolderNode,offset=0){if(node.disabled||treeBusy.has(node.id)||(node.loaded&&!offset))return;treeBusy.add(node.id);expanded.add(node.id);try{const d=await props.api(`/cloud/files?cid=${node.id}&offset=${offset}&limit=100`);const children:FolderNode[]=d.items.filter((e:any)=>e.is_dir).map((e:any)=>({...e,trail:[...(node.trail||[]),{id:e.id,name:e.name}],children:[{id:'placeholder:'+e.id,name:'展开读取目录',disabled:true}]}));if(d.has_more)children.push({id:'more:'+node.id+':'+(d.next_offset??offset+d.items.length),name:'加载更多目录…',more:true,parent:node.id,offset:d.next_offset??offset+d.items.length});treeData.value=replaceNode(node.id,{loaded:true,children:offset?[...(node.children||[]).filter(n=>!n.more&&!n.disabled),...children]:children});await nextTick();tree.value?.setExpandedKeys([...expanded])}catch(e){error.value=(e as Error).message}finally{treeBusy.delete(node.id)}}
async function treeSelect(node:FolderNode){if(node.disabled)return;if(node.more){tree.value?.setCurrentKey(cid.value);const parent=findNode(node.parent!);if(parent)await expandFolder(parent,node.offset);return}await Promise.all([browse(node.id,undefined,undefined,0,node.trail||[{id:'0',name:'115 网盘'},{id:node.id,name:node.name}]),expandFolder(node)])}
async function revealTree(sequence=browseSequence){if(window.innerWidth<=760)return;for(const folder of path.value.slice(0,-1)){if(sequence!==browseSequence)return;const node=findNode(folder.id);if(node){expanded.add(node.id);await expandFolder(node)}}await nextTick();if(sequence===browseSequence){tree.value?.setExpandedKeys([...expanded]);tree.value?.setCurrentKey(cid.value)}}
watch(configured,async value=>{if(value){await nextTick();await browse('0',undefined,0);if(window.innerWidth>760)await expandFolder(treeData.value[0])}})
function resizeTree(){treeHeight.value=420;if(window.innerWidth>760&&configured.value){void expandFolder(treeData.value[0]).then(()=>revealTree())}}
onMounted(()=>window.addEventListener('resize',resizeTree));onUnmounted(()=>{browseSequence++;window.removeEventListener('resize',resizeTree)})
const maint=ref<any>({sign_enabled:false,sign_cron:'0 9 * * *',recycle_enabled:false,recycle_cron:'0 4 * * 0',recycle_password:'000000'})
const maintMeta=ref<any>({last_sign_at:0,last_clear_at:0,sign_due:0,recycle_due:0}),maintLoaded=ref(false),signBusy=ref(false),clearBusy=ref(false),recycleInfo=ref<any>(null),signMsg=ref('')
async function loadMaint(){try{const r=await props.api('/cloud/maintenance');maint.value={...maint.value,...r.config};maintMeta.value=r;maintLoaded.value=true}catch(e){maintLoaded.value=false;error.value='签到与回收站设置读取失败：'+(e as Error).message}}
async function saveMaint(){if(!maintLoaded.value)return;await run(async()=>{const r=await props.api('/cloud/maintenance','PUT',maint.value);maintMeta.value=r;ElMessage.success('定时设置已保存')})}
async function doSign(){signBusy.value=true;signMsg.value='';try{const r=await props.api('/cloud/sign','POST');signMsg.value=(r.message||'签到完成')+(r.days!=null?` 连续 ${r.days} 天`:'')+(r.reward!=null?`，今日获得 ${r.reward} 枫叶`:'')+(r.balance!=null?`，当前余额 ${r.balance} 枫叶`:'');ElMessage.success(signMsg.value);await loadMaint()}catch(e){signMsg.value=(e as Error).message}finally{signBusy.value=false}}
async function loadRecycle(){try{recycleInfo.value=await props.api('/cloud/recycle')}catch(e){recycleInfo.value={available:false,error:(e as Error).message}}}
async function doClear(){try{await ElMessageBox.confirm('回收站清空后无法恢复，确定继续？','清空回收站',{confirmButtonText:'清空',cancelButtonText:'取消',type:'warning'})}catch{return}clearBusy.value=true;try{const r=await props.api('/cloud/recycle/clear','POST');ElMessage.success(r.message||'已清空');await Promise.all([loadRecycle(),loadMaint()])}catch(e){error.value=(e as Error).message}finally{clearBusy.value=false}}
const recycleCount=computed(()=>{const i=recycleInfo.value&&recycleInfo.value.info||{};const v=i.count??i.num??i.total??i.size;return v==null?'—':v})
function fmtTime(ts:number){if(!ts)return '—';const d=new Date(ts*1000);const p=(n:number)=>String(n).padStart(2,'0');return `${d.getMonth()+1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`}

onMounted(async()=>{try{configured.value=(await props.api('/settings')).cookie_configured}catch(e){error.value=(e as Error).message};void loadAccount();void loadMaint();void loadRecycle()})
</script>
<template><div class="page-title"><div><h1>115 网盘</h1><p class="muted">管理账号、浏览目录、签到与回收站。</p></div></div><el-alert v-if="error" :title="error" type="error"/>
<section class="panel account-panel">
  <div class="section-title"><div><h2>115 账号</h2><p class="account-caption">管理登录状态与网盘容量</p></div><el-tag :type="status==='已登录'?'success':'info'">{{configured?status:'尚未登录'}}</el-tag></div>
  <div v-if="account&&!account.error" class="account-card"><div class="account-row"><div class="account-avatar"><img v-if="account.avatar" :src="account.avatar" alt=""/><span v-else>115</span></div><div class="account-id"><strong>{{account.nickname||'115 账号'}}</strong><small>UID {{account.uid||'—'}}</small></div><el-tag v-if="account.vip_level" type="warning" effect="light">VIP</el-tag><el-button text :loading="accountBusy" @click="loadAccount(true)">刷新容量</el-button></div><div class="usage"><div class="usage-head"><span>已用 {{tb(account.space_used)}}</span><span class="muted">共 {{tb(account.space_total)}}</span></div><div class="usage-bar"><i :style="{width:pct(account.space_used,account.space_total)+'%'}"/></div></div></div>
  <el-alert v-else-if="account&&account.error" :title="account.error" type="warning" :closable="false"/>
  <div class="button-row account-actions"><el-button type="primary" @click="qr=true">扫码登录</el-button><el-button :loading="busy" @click="check">检查登录</el-button></div>
  <details class="cookie-options"><summary>手动填写 Cookie</summary><p class="hint">也可以手动填写登录信息，留空不会覆盖已保存的值。</p><el-form label-position="top"><el-form-item label="Cookie"><el-input v-model="cookie" type="password" autocomplete="new-password" show-password :placeholder="configured?'已加密保存，留空保留原值':'UID、CID、SEID'"/></el-form-item></el-form><el-button :disabled="!cookie||busy" @click="saveCookie">保存 Cookie</el-button></details>
</section>
<section class="panel">
  <div class="section-title maint-section-title"><div><h2>签到与回收站</h2><p class="account-caption">每日签到、按计划清空回收站</p></div><el-button type="primary" :loading="busy" :disabled="!maintLoaded" @click="saveMaint">保存定时设置</el-button></div>
  <div v-if="!maintLoaded" class="button-row"><el-button @click="loadMaint">重新读取设置</el-button></div>
  <div class="maint-grid">
    <div class="maint-card">
      <div class="maint-head"><strong>每日签到</strong><el-button type="primary" size="small" :loading="signBusy" :disabled="!configured" @click="doSign">立即签到</el-button></div>
      <p class="hint">上次签到：{{fmtTime(maintMeta.last_sign_at)}}<span v-if="signMsg"> · {{signMsg}}</span></p>
      <div class="maint-row"><label>定时签到</label><el-switch v-model="maint.sign_enabled"/></div>
      <div class="maint-row" v-if="maint.sign_enabled"><label>Cron</label><el-input v-model="maint.sign_cron" size="small" placeholder="0 9 * * *" style="max-width:160px"/></div>
    </div>
    <div class="maint-card">
      <div class="maint-head"><strong>回收站</strong><el-button size="small" :disabled="!configured" @click="loadRecycle">刷新</el-button></div>
      <p class="hint" v-if="recycleInfo&&recycleInfo.available">回收站条目：{{recycleCount}} · 上次清空：{{fmtTime(maintMeta.last_clear_at)}}</p>
      <p class="hint" v-else-if="recycleInfo&&recycleInfo.error">读取失败：{{recycleInfo.error}}</p>
      <div class="maint-row"><label>定时清空</label><el-switch v-model="maint.recycle_enabled"/></div>
      <div class="maint-row" v-if="maint.recycle_enabled"><label>Cron</label><el-input v-model="maint.recycle_cron" size="small" placeholder="0 4 * * 0" style="max-width:160px"/></div>
      <div class="maint-row"><label>安全密钥</label><el-input v-model="maint.recycle_password" type="password" show-password size="small" maxlength="6" placeholder="6 位数字" style="max-width:160px"/></div>
      <div class="button-row"><el-button type="danger" plain size="small" :loading="clearBusy" :disabled="!configured" @click="doClear">立即清空回收站</el-button></div>
    </div>
  </div>
</section>
<section class="panel cloud-browser-panel"><div class="section-title"><h2>网盘目录与文件</h2><el-button :disabled="listLoading||!configured" @click="browse()">刷新当前目录</el-button></div>
<div v-if="configured" class="cloud-browser">
  <aside class="cloud-tree-pane" aria-label="目录树"><el-tree-v2 ref="tree" :data="treeData" :height="treeHeight" :item-size="34" :props="{value:'id',label:'name',children:'children',disabled:'disabled'}" :default-expanded-keys="['0']" :expand-on-click-node="false" highlight-current @node-expand="(node:FolderNode)=>expandFolder(node)" @node-collapse="(node:FolderNode)=>expanded.delete(node.id)" @node-click="treeSelect"/></aside>
  <div class="cloud-files">
    <nav class="cloud-breadcrumbs" aria-label="当前目录"><template v-for="(p,i) in path" :key="p.id"><span v-if="i" class="cloud-path-divider" aria-hidden="true">/</span><button :aria-current="i===path.length-1?'location':undefined" @click="browse(p.id,undefined,i)">{{p.name}}</button></template></nav>
    <div class="cloud-list-toolbar"><el-button :disabled="listLoading||path.length<2" @click="browse(path[path.length-2].id,undefined,path.length-2)">返回上级</el-button><span class="muted">{{files.length}}{{more?'＋':''}} 项</span></div>
    <CloudEntryList :items="files" :loading="listLoading" @open="r=>browse(r.id,r.name)"/>
    <div v-if="offset||more" class="cloud-pagination"><el-button :disabled="listLoading||!offset" @click="browse(cid,undefined,undefined,Math.max(0,offset-100))">上一页</el-button><span class="muted">第 {{Math.floor(offset/100)+1}} 页</span><el-button :disabled="listLoading||!more" @click="browse(cid,undefined,undefined,next)">下一页</el-button></div>
  </div>
</div><el-empty v-else description="登录 115 后即可浏览目录"/></section>
<CloudLoginDialog v-model="qr" :api="api" @connected="saved"/></template>
<style scoped>
.cloud-browser :deep(.cloud-entry-list){min-height:300px}

.account-card{margin-top:18px;padding-top:16px;border-top:1px solid var(--el-border-color-lighter,#e6ebf2);display:grid;gap:12px}
.account-row{display:flex;align-items:center;gap:12px}
.account-avatar{width:42px;height:42px;border-radius:50%;overflow:hidden;background:var(--el-fill-color,#eef2f8);display:grid;place-items:center;font-size:12px;color:var(--el-text-color-secondary);flex-shrink:0}
.account-avatar img{width:100%;height:100%;object-fit:cover}
.account-id{display:grid;gap:2px;min-width:0;flex:1}
.account-id strong{font-size:14px;font-weight:600}
.account-id small{font-size:11px;color:var(--el-text-color-secondary)}
.usage{display:grid;gap:6px}
.usage-head{display:flex;justify-content:space-between;font-size:12px}
.usage-bar{height:8px;border-radius:6px;background:var(--el-fill-color,#eef2f8);overflow:hidden}
.usage-bar i{display:block;height:100%;border-radius:6px;background:var(--el-color-primary)}
</style>

<style scoped>

.account-card{padding:18px;margin-top:22px;border:1px solid var(--line);border-radius:10px;background:var(--surface-soft);gap:17px}.account-avatar{width:48px;height:48px;border:2px solid var(--surface);box-shadow:0 2px 8px #1b376014}.account-id strong{font-size:15px}.account-id small{margin-top:3px}.usage-head{font-size:12px;font-weight:600}.usage-bar{height:7px}.usage-bar i{background:linear-gradient(90deg,var(--accent),#73a9ff)}.cloud-browser-panel :deep(.cloud-entry){border-radius:7px;padding-inline:10px}.cloud-browser-panel :deep(.cloud-entry:hover){background:var(--surface-soft)}.cloud-browser-panel :deep(.cloud-entry-icon){width:22px;height:22px}.cloud-breadcrumbs{padding:7px 3px 14px}.cloud-list-toolbar{padding:0 2px}.cloud-tree-pane{padding:10px 4px;background:var(--surface-soft)}
@media(max-width:760px){.account-row{flex-wrap:wrap}.account-row .el-button{margin-left:auto}.account-card{padding:14px}.cloud-browser-panel :deep(.cloud-entry){min-height:52px}}
.account-caption{margin:5px 0 0;color:var(--muted);font-size:12px}.account-actions{margin-top:16px}.cookie-options{margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}.cookie-options summary{cursor:pointer;color:var(--accent);font-size:13px;font-weight:600}.cookie-options summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}.cookie-options .hint{margin:12px 0}.cookie-options :deep(.el-form-item){margin-bottom:12px}
.maint-section-title{flex-wrap:nowrap}.maint-section-title>div{flex:1}.maint-section-title>.el-button{flex-shrink:0}.maint-grid{display:grid;grid-template-columns:minmax(0,1fr);gap:14px;margin:14px 0}.maint-card{border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:10px}.maint-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.maint-head strong{font-size:14px}.maint-row{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px}.maint-row label{color:var(--text)}.maint-card .hint{margin:0;font-size:12px;line-height:1.6;color:var(--muted);word-break:break-word;overflow-wrap:anywhere}@media(min-width:760px){.maint-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}</style>
