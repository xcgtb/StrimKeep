<script setup lang="ts">
import {onMounted,ref} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const servers=ref<any[]>([]),busy=ref(false),error=ref(''),plays=ref<any[]>([]),playsFor=ref(''),playsOpen=ref(false)

function reasonText(raw:string){
  const labels:Record<string,string> = {no_sources:'Emby 未返回可播放媒体源',no_identity:'媒体源不是本项目的播放地址',share_missing:'分享记录不存在',share_revoked:'分享记录已撤销',timeout:'115 取链超时', 'cloud:RATE_LIMITED':'115 限流', 'cloud:LOGIN_REQUIRED':'115 未登录', 'cloud:LOGIN_INVALID':'115 登录失效', 'cloud:SHARE_NOT_FOUND':'分享或文件已失效', 'cloud:FILE_NOT_FOUND':'网盘文件不存在'}
  return (raw || '').split(',').filter(Boolean).map(code => labels[code] || code).join('；')
}
function stateOf(s:any){return s.running?'运行中':(s.enabled?'已启用，但没有监听（端口被占用？）':'已停用')}
async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function sync(list:any[]){servers.value=(list||[]).map((s:any)=>({...s,upstream_draft:s.upstream||'',api_key_draft:'',port_draft:s.port}))}
async function load(){try{const r:any=await props.api('/emby-proxy');sync(r.servers)}catch(e){error.value=(e as Error).message}}
async function add(){await run(async()=>{const r:any=await props.api('/emby-proxy/servers','POST');sync(r.servers);ElMessage.success('已添加一台，默认是停用状态')})}
async function save(s:any){await run(async()=>{const r:any=await props.api('/emby-proxy/servers/'+s.id,'PUT',{name:s.name,port:Number(s.port_draft)||9001});sync(r.servers);ElMessage.success('已保存')})}
async function toggle(s:any){await run(async()=>{const r:any=await props.api('/emby-proxy/servers/'+s.id+(s.enabled?'/disable':'/enable'),'POST');sync(r.servers);ElMessage.success(s.enabled?'已停用':'已启用')})}
async function probe(s:any){await run(async()=>{const r:any=await props.api('/emby-proxy/servers/'+s.id+'/test','POST');ElMessage({type:r.ok?'success':'error',message:r.message+(r.latency_ms?('，'+r.latency_ms+' ms'):'')})})}
async function remove(s:any){await run(async()=>{await ElMessageBox.confirm('移除后这个端口不再监听；已经生成的 STRM 不受影响。','移除「'+s.name+'」',{type:'warning'});const r:any=await props.api('/emby-proxy/servers/'+s.id,'DELETE');sync(r.servers);ElMessage.success('已移除')})}
async function showPlays(s:any){await run(async()=>{playsFor.value=s.name;const r:any=await props.api('/emby-proxy/servers/'+s.id+'/plays');plays.value=r.plays||[];playsOpen.value=true})}
onMounted(load)
</script>
<template>
<div class="proxy-block">
  <div class="proxy-head">
    <div>
      <h3>Emby 反代</h3>
      <p class="hint">播放器指到这里的端口时，StrimKeep 会尝试在 PlaybackInfo 中补充 115 直链。成功时播放器可直接向 CDN 拉取；取链失败时可能回退到 Emby 播放并使用隧道带宽。后端使用上面的「Emby 服务连接」。</p>
    </div>
    <el-button :loading="busy" @click="add">添加一台</el-button>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" style="margin-bottom:12px"/>
  <div v-for="s in servers" :key="s.id" class="proxy-card">
    <div class="proxy-card-head">
      <el-input v-model="s.name" class="proxy-name" maxlength="40" aria-label="名称"/>
      <span class="proxy-state" :class="s.running?'on':(s.enabled?'warn':'off')">{{stateOf(s)}}</span>
      <div class="proxy-actions">
        <el-button size="small" type="success" plain :loading="busy" @click="save(s)">保存</el-button>
        <el-button size="small" :type="s.enabled?'danger':'primary'" plain :loading="busy" @click="toggle(s)">{{s.enabled?'停用':'启用'}}</el-button>
        <el-button size="small" plain :loading="busy" @click="showPlays(s)">取链记录</el-button>
        <el-button size="small" plain :loading="busy" @click="probe(s)">测试连通性</el-button>
        <el-button size="small" plain type="danger" :loading="busy" @click="remove(s)">移除</el-button>
      </div>
    </div>
    <div class="proxy-field"><label>端口</label><el-input-number v-model="s.port_draft" :min="1" :max="65535"/><span class="hint">播放器填 http://服务器地址:端口；容器是 bridge 网络时要另外映射这个端口。地址与 Key 用上面的「Emby 服务连接」。</span></div>
  </div>
  <p v-if="!servers.length" class="hint">还没有反代服务器，点右上角「添加一台」。</p>
  <el-dialog v-model="playsOpen" :title="'取链记录 · '+playsFor" width="720px">
    <el-table :data="plays" size="small" empty-text="还没有取链记录">
      <el-table-column label="时间" width="170"><template #default="c">{{new Date(c.row.at*1000).toLocaleString()}}</template></el-table-column>
      <el-table-column prop="user_agent" label="客户端" width="150"/>
      <el-table-column prop="target" label="请求" show-overflow-tooltip/>
      <el-table-column prop="filled" label="直链" width="70"/>
      <el-table-column label="结果" width="90"><template #default="c">{{c.row.outcome==='ok'?'已注入':c.row.outcome==='empty'?'无需注入':'失败'}}</template></el-table-column>
      <el-table-column label="原因" min-width="150" show-overflow-tooltip><template #default="c">{{reasonText(c.row.reason)}}</template></el-table-column>
    </el-table>
  </el-dialog>
</div>
</template>
<style scoped>
.proxy-block{margin-top:8px;padding-top:16px;border-top:1px solid var(--line)}
.proxy-head{display:flex;align-items:flex-start;gap:16px;margin-bottom:12px}
.proxy-head h3{margin:0 0 6px;font-size:15px}
.proxy-card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px;background:var(--el-fill-color-blank)}
.proxy-card-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.proxy-name{max-width:220px}
.proxy-state{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line)}
.proxy-state.on{color:var(--el-color-success);border-color:var(--el-color-success)}
.proxy-state.warn{color:var(--el-color-warning);border-color:var(--el-color-warning)}
.proxy-state.off{color:var(--el-text-color-secondary)}
.proxy-actions{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}
.proxy-field{margin-bottom:12px}
.proxy-field>label{display:block;font-size:13px;font-weight:600;color:var(--el-text-color-regular);margin-bottom:6px}
.proxy-field .hint{display:block;margin-top:6px}
</style>
