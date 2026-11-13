<script setup lang="ts">
import {onMounted,ref} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import DirectoryPicker from './DirectoryPicker.vue'
type Profile={id?:string;name:string;source_cid:string;source_label:string;mode:'incremental'|'full';enabled:boolean;frequency:'daily'|'weekly'|'interval'|'cron';weekday:number;time:string;interval_minutes:number;cron:string;timezone:string;next_due?:number}
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const profiles=ref<Profile[]>([]),draft=ref<Profile|null>(null),picker=ref(false),busy=ref(false),error=ref('')
const weekdays=['周日','周一','周二','周三','周四','周五','周六']
const blank=():Profile=>({name:'',source_cid:'',source_label:'',mode:'incremental',enabled:false,frequency:'daily',weekday:1,time:'03:00',interval_minutes:60,cron:'0 3 * * *',timezone:'Asia/Shanghai'})
async function load(){profiles.value=(await props.api('/sync/profiles')).items}
async function runAction(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
onMounted(()=>runAction(load))
function select(folder:{id:string,name:string}){if(draft.value){draft.value.source_cid=folder.id;draft.value.source_label=folder.name}picker.value=false}
async function save(){if(!draft.value)return;const p=draft.value;if(!p.name.trim()||!p.source_cid){ElMessage.warning('请填写任务名称并选择一个目录');return}await runAction(async()=>{const body={name:p.name.trim(),source_cid:p.source_cid,source_label:p.source_label,mode:p.mode,enabled:p.enabled,frequency:p.frequency,weekday:p.weekday,time:p.time,interval_minutes:p.interval_minutes??60,cron:p.cron||'0 3 * * *',timezone:p.timezone||'Asia/Shanghai'};await props.api(p.id?`/sync/profiles/${p.id}`:'/sync/profiles',p.id?'PUT':'POST',body);draft.value=null;await load();ElMessage.success('同步任务已保存')})}
async function execute(p:Profile){await runAction(async()=>{const job=await props.api(`/sync/profiles/${p.id}/run`,'POST',{});ElMessage.success(`“${p.name}”已加入队列（任务 #${job.id}）`)})}
async function remove(p:Profile){try{await ElMessageBox.confirm(`删除同步任务“${p.name}”？已入队的任务不会取消，云盘与本地文件不会被删除。`,'删除同步任务',{type:'warning'})}catch{return}await runAction(async()=>{await props.api(`/sync/profiles/${p.id}`,'DELETE');await load();ElMessage.success('同步任务已删除')})}
function timing(p:Profile){if(!p.enabled)return '仅手动';if(p.frequency==='interval')return `每 ${p.interval_minutes} 分钟`;if(p.frequency==='cron')return `Cron ${p.cron}`;return `${p.frequency==='daily'?'每天':weekdays[p.weekday]} ${p.time}`}
</script>
<template>
  <el-alert v-if="error" :title="error" type="error" show-icon/>
  <div class="sync-intro"><div><strong>按目录管理同步</strong><p>保存一次，之后点击对应任务的“立即执行”。</p></div><el-button type="primary" :disabled="busy" @click="draft=blank()">＋ 新建任务</el-button></div>
  <section v-if="draft" class="sync-editor">
    <div class="sync-editor-title"><div><strong>{{draft.id?'编辑任务':'新建同步任务'}}</strong><p>一个任务对应一个目录，可分别设置增量和全量任务。</p></div><el-button text @click="draft=null">取消</el-button></div>
    <div class="sync-editor-grid">
      <label class="sync-field"><span>任务名称</span><el-input v-model="draft.name" maxlength="80" placeholder="例如：AV 每天增量"/></label>
      <div class="sync-field"><span>同步目录</span><div class="sync-folder"><span>{{draft.source_label||'选择 115 目录'}}</span><el-button @click="picker=true">{{draft.source_cid?'更换':'选择目录'}}</el-button></div></div>
      <div class="sync-field"><span>同步方式</span><el-radio-group v-model="draft.mode"><el-radio-button value="incremental">增量同步</el-radio-button><el-radio-button value="full">全量重建</el-radio-button></el-radio-group></div>
      <div class="sync-field"><span>定时执行</span><div class="sync-switch"><el-switch v-model="draft.enabled"/><span>{{draft.enabled?'已开启':'关闭（仍可手动执行）'}}</span></div></div>
      <template v-if="draft.enabled"><div class="sync-field wide"><span>执行频率</span><el-radio-group v-model="draft.frequency"><el-radio-button value="daily">每天</el-radio-button><el-radio-button value="weekly">每周</el-radio-button><el-radio-button value="interval">间隔</el-radio-button><el-radio-button value="cron">Cron</el-radio-button></el-radio-group></div>
        <div v-if="draft.frequency==='weekly'" class="sync-field"><span>星期</span><el-select v-model="draft.weekday"><el-option v-for="(day,i) in weekdays" :key="i" :label="day" :value="i"/></el-select></div>
        <label v-if="draft.frequency==='daily'||draft.frequency==='weekly'" class="sync-field"><span>执行时间</span><el-input v-model="draft.time" type="time"/></label>
        <div v-if="draft.frequency==='interval'" class="sync-field"><span>间隔（分钟）</span><el-input-number v-model="draft.interval_minutes" :min="5" :max="43200"/></div>
        <label v-if="draft.frequency==='cron'" class="sync-field"><span>Cron 表达式</span><el-input v-model="draft.cron" placeholder="0 3 * * *"/></label>
        <div class="sync-field"><span>时区</span><el-select v-model="draft.timezone"><el-option label="北京时间" value="Asia/Shanghai"/><el-option label="UTC" value="UTC"/></el-select></div>
      </template>
    </div>
    <div class="sync-editor-actions"><span>增量适合日常新增；全量逐项核对。</span><el-button :disabled="busy" @click="draft=null">取消</el-button><el-button type="primary" :disabled="busy" @click="save">保存任务</el-button></div>
  </section>
  <div v-if="profiles.length" class="sync-list"><article v-for="p in profiles" :key="p.id" class="sync-card"><div class="sync-card-top"><strong>{{p.name}}</strong><span class="sync-badge" :class="{full:p.mode==='full'}">{{p.mode==='incremental'?'增量同步':'全量重建'}}</span></div><div class="sync-card-directory">{{p.source_label}}</div><div class="sync-card-meta"><span>{{timing(p)}}</span><span v-if="p.enabled&&p.next_due">下次 {{new Date(p.next_due*1000).toLocaleString('zh-CN',{timeZone:p.timezone||'Asia/Shanghai'})}}</span></div><div class="sync-card-actions"><el-button type="primary" :disabled="busy" @click="execute(p)">立即执行</el-button><el-button :disabled="busy" @click="draft={...blank(),...p}">编辑</el-button><el-button text type="danger" :disabled="busy" @click="remove(p)">删除</el-button></div></article></div>
  <div v-else-if="!draft" class="sync-empty"><strong>还没有同步任务</strong><p>例如创建“影视库每天增量”或“AV 每周全量”。</p><el-button @click="draft=blank()">创建第一个任务</el-button></div>
  <el-button text @click="emit('navigate','tasks')">查看任务进度 →</el-button>
  <DirectoryPicker v-if="picker" :api="api" @select="select" @close="picker=false"/>
</template>
<style scoped>
.sync-intro,.sync-editor-title,.sync-card-top,.sync-editor-actions{display:flex;align-items:center;justify-content:space-between;gap:16px}.sync-intro{padding:8px 0 18px}.sync-intro strong,.sync-editor-title strong{color:var(--text);font-size:15px}.sync-intro p,.sync-editor-title p,.sync-empty p{color:var(--muted);font-size:12px;margin:6px 0 0}.sync-editor{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px}.sync-editor-title{padding-bottom:18px;border-bottom:1px solid var(--line)}.sync-editor-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 22px;padding:20px 0}.sync-field{display:flex;flex-direction:column;gap:8px;min-width:0;color:var(--text);font-size:13px}.sync-field.wide{grid-column:1/-1}.sync-folder{display:flex;align-items:center;justify-content:space-between;gap:8px;border:1px solid var(--line);border-radius:5px;min-height:32px;padding-left:10px}.sync-folder span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}.sync-switch{display:flex;align-items:center;gap:10px;min-height:32px}.sync-editor-actions{border-top:1px solid var(--line);padding-top:16px;justify-content:flex-end}.sync-editor-actions>span{margin-right:auto;color:var(--muted);font-size:12px}.sync-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,310px),1fr));gap:14px;margin-bottom:14px}.sync-card{border:1px solid var(--line);background:var(--surface);border-radius:14px;padding:18px;min-width:0}.sync-card-top strong{font-size:15px;color:var(--text);overflow-wrap:anywhere}.sync-badge{background:#eaf2ff;color:#2563eb;border-radius:999px;padding:4px 9px;font-size:12px;white-space:nowrap}.sync-badge.full{background:#fff2df;color:#a45b00}.sync-card-directory{color:var(--muted);font-size:13px;line-height:1.6;overflow-wrap:anywhere;margin:12px 0}.sync-card-meta{display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--muted);font-size:12px;min-height:24px}.sync-card-actions{display:flex;align-items:center;border-top:1px solid var(--line);padding-top:14px;margin-top:10px}.sync-empty{text-align:center;border:1px dashed var(--line);border-radius:14px;padding:28px 12px;margin-bottom:12px}.sync-empty .el-button{margin-top:16px}@media(max-width:700px){.sync-editor-grid{grid-template-columns:1fr}.sync-field.wide{grid-column:auto}.sync-editor-actions{flex-wrap:wrap}.sync-editor-actions>span{width:100%}.sync-intro{align-items:flex-start}}
</style>
