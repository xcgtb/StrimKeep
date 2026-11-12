<script setup lang="ts">
import {useVisiblePolling} from './pageActivity'
import {ref,onMounted,onUnmounted,computed} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import TransferDirectories from './TransferDirectories.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const config=ref({target_cid:'',target_label:'',auto_organize:false,bot_auto_organize:false,bot_enabled:false,allowed_user_id:''})
const baseline=ref(''),ready=ref(false),busy=ref(false),error=ref(''),text=ref(''),jobs=ref<any[]>([]),bot=ref<any>({}),directoryEditor=ref<any>(null)
const settingsOpen=ref(false)
const dirty=computed(()=>ready.value&&JSON.stringify(config.value)!==baseline.value)
const states:Record<string,string>={queued:'排队中',running:'正在转存',completed:'转存完成',paused:'已暂停',cancelled:'已取消',needs_review:'转存结果待核查',waiting_login:'需要登录 115'}

async function run(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function refresh(){jobs.value=(await props.api('/transfer/jobs')).items;bot.value=(await props.api('/transfer/config')).bot}
async function destinationsSaved(){if(dirty.value){ElMessage.warning('下方转存设置有未保存改动，请先保存或刷新后再修改目录');return}const r=await props.api('/transfer/config');config.value=r.config;baseline.value=JSON.stringify(config.value)}
async function save(){await run(async()=>{const r=await props.api('/transfer/config','PUT',config.value);config.value=r.config;baseline.value=JSON.stringify(config.value);ElMessage.success('转存设置已保存')})}
async function start(){await run(async()=>{const r=await props.api('/transfer/jobs','POST',{text:text.value});text.value='';ElMessage.success(`${r.items.length} 个转存任务已排队`);await refresh()})}
async function canLeave(){if(directoryEditor.value&&!await directoryEditor.value.canLeave())return false;if(!dirty.value&&!text.value.trim())return true;try{await ElMessageBox.confirm('设置或分享链接还没有保存。','离开转存页面？',{confirmButtonText:'离开',cancelButtonText:'继续编辑'});return true}catch{return false}}
defineExpose({canLeave})
onMounted(()=>{run(async()=>{const r=await props.api('/transfer/config');config.value=r.config;baseline.value=JSON.stringify(config.value);ready.value=true;await refresh()})})
onUnmounted(()=>{})
useVisiblePolling(async()=>{if(!busy.value)await refresh()},5000)
</script>
<template>
<div class="page-heading"><div><h1>115 转存</h1><p class="muted">粘贴分享链接，保存到指定目录。</p></div></div>
<el-alert v-if="error" :title="error" type="error" show-icon class="global-error"/>
<section class="panel transfer-entry"><h2>分享链接</h2><el-input v-model="text" type="textarea" :rows="5" placeholder="粘贴 115 分享链接，可带 password 或“提取码”。一次最多 10 个链接。" aria-label="115 分享链接"/>
<p class="hint">保存位置：{{config.target_label||config.target_cid||'请先选择目标目录'}} · {{config.auto_organize?'转存后继续整理、生成 STRM':'仅保存到网盘'}}</p>
<div class="button-row"><el-button type="primary" :disabled="busy||dirty||!ready||!config.target_cid||!text.trim()" @click="start">开始转存</el-button><span v-if="dirty" class="hint">请先保存下方设置。</span></div></section>
<TransferDirectories ref="directoryEditor" :api="api" :disabled="dirty||busy" @saved="destinationsSaved" @navigate="page=>emit('navigate',page)"/>
<section class="panel transfer-options"><div class="settings-heading"><div><h2>转存设置 <span v-if="dirty" class="unsaved">未保存</span></h2><p>自动整理与 Telegram 机器人</p></div><el-button @click="settingsOpen=!settingsOpen">{{settingsOpen?'收起':'管理设置'}}</el-button></div><div v-if="settingsOpen" class="settings-content">
<p class="hint">直接保存到所选目录，不额外创建任务文件夹。自动整理只处理这次收到的内容。</p>
<div class="workflow-switch-row"><div><strong>Web 转存后立即整理</strong><p>开启后落盘完成立即入队整理（目标目录必须在待整理范围内）；关闭时只落盘，由「影视整理」的手动/定时扫描决定。</p></div><el-switch v-model="config.auto_organize" :disabled="busy" aria-label="Web 转存后立即整理"/></div>
<h3>Telegram 接收链接</h3><div class="workflow-switch-row"><div><strong>机器人转存后立即整理</strong><p>与 Web 分开设置：开启后机器人提交的转存落盘完成就入队整理；关闭时只落盘。</p></div><el-switch v-model="config.bot_auto_organize" :disabled="busy" aria-label="机器人转存后立即整理"/></div><div class="workflow-switch-row"><div><strong>启用转存机器人</strong><p>私聊机器人发送链接即可转存。复用通知页的 Bot Token。</p></div><el-switch v-model="config.bot_enabled" :disabled="busy" aria-label="启用转存机器人"/></div>
<el-form label-position="top"><el-form-item label="允许操作的 TG 用户数字 ID"><el-input v-model="config.allowed_user_id" placeholder="仅此用户可以私聊机器人发起转存"/></el-form-item></el-form>
<p v-if="config.bot_enabled" class="hint">{{bot.state==='ready'?'机器人已连接，可发送新链接':bot.state==='error'?bot.message:'机器人正在连接，请稍候再发链接'}}。启用前的旧消息不会自动执行。</p>
<p class="hint">直接发分享链接使用机器人独立目录；发送 /save 目录ID 分享链接 可指定其他目录。自动整理开启时，目标仍须为待整理目录。</p>
<div class="button-row"><el-button type="primary" :disabled="busy||!ready||!dirty" @click="save">保存设置</el-button><el-button :disabled="busy" @click="emit('navigate','system')">配置 Telegram 通知</el-button></div></div></section>
<section class="panel transfer-history"><div class="history-heading"><div><h2>最近转存</h2><p>查看最近提交的任务</p></div><el-button text type="primary" @click="emit('navigate','tasks')">查看全部</el-button></div>
<p v-if="!jobs.length" class="history-empty">还没有转存记录</p>
<div v-for="job in jobs.slice(0,3)" :key="job.id" class="history-row"><span class="history-id">#{{job.id}}</span><div class="history-main"><strong :title="job.config.target_label||'115 转存'">{{job.config.target_label||'115 转存'}}</strong><span>{{job.config.origin==='telegram'?'Telegram 机器人':'Web'}} · {{job.message||job.stage}}</span></div><el-tag size="small" :type="job.state==='completed'?'success':job.state==='running'?'primary':job.state==='needs_review'?'warning':'info'" effect="light">{{states[job.state]||job.state}}</el-tag></div></section>
</template>

<style scoped>

.transfer-entry :deep(.el-textarea__inner){min-height:145px;padding:14px 16px;background:var(--surface-soft);box-shadow:0 0 0 1px var(--line) inset}.transfer-entry .hint{margin-top:12px}.settings-heading,.history-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.settings-heading h2,.history-heading h2{margin:0;font-size:16px}.settings-heading p,.history-heading p{font-size:12px;color:var(--muted);margin:5px 0 0}.settings-content{margin-top:18px;padding-top:4px;border-top:1px solid var(--line)}.unsaved{font-size:11px;color:#b77b2b;margin-left:6px;font-weight:400}.transfer-options h3{margin-top:24px}.history-empty{color:var(--muted);font-size:13px;margin:20px 0 2px}.history-row{display:flex;align-items:center;gap:12px;padding:13px 0;border-top:1px solid var(--line)}.history-row:first-of-type{margin-top:14px}.history-id{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;flex:none}.history-main{display:grid;gap:4px;flex:1;min-width:0}.history-main strong,.history-main span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.history-main strong{font-size:13px}.history-main span{font-size:12px;color:var(--muted)}
@media(max-width:760px){.transfer-entry :deep(.el-textarea__inner){min-height:125px}.transfer-options .workflow-switch-row{align-items:flex-start}.transfer-history .job-card{padding:14px}}
@media(max-width:760px){.history-row{align-items:flex-start;flex-wrap:wrap}.history-main{flex-basis:65%}.history-main strong,.history-main span{white-space:normal;overflow-wrap:anywhere;line-height:1.5}.history-row :deep(.el-tag){margin-left:auto}}
</style>
