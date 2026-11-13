<script setup lang="ts">
import {onMounted,ref} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import DirectoryPicker from './DirectoryPicker.vue'

type Profile={id?:string;name:string;source_cid:string;source_label:string;mode:'incremental'|'full';enabled:boolean;frequency:'daily'|'weekly';weekday:number;time:string;next_due?:number}
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const profiles=ref<Profile[]>([]),draft=ref<Profile|null>(null),picker=ref(false),busy=ref(false),error=ref('')
const weekdays=['周日','周一','周二','周三','周四','周五','周六']
const blank=():Profile=>({name:'',source_cid:'',source_label:'',mode:'incremental',enabled:false,frequency:'daily',weekday:1,time:'03:00'})
async function load(){profiles.value=(await props.api('/sync/profiles')).items}
async function runAction(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
onMounted(()=>runAction(load))
function select(folder:{id:string,name:string}){if(draft.value){draft.value.source_cid=folder.id;draft.value.source_label=folder.name}picker.value=false}
async function save(){if(!draft.value)return;const p=draft.value;if(!p.name.trim()||!p.source_cid){ElMessage.warning('请填写任务名称并选择一个目录');return}await runAction(async()=>{const body={name:p.name.trim(),source_cid:p.source_cid,source_label:p.source_label,mode:p.mode,enabled:p.enabled,frequency:p.frequency,weekday:p.weekday,time:p.time};await props.api(p.id?`/sync/profiles/${p.id}`:'/sync/profiles',p.id?'PUT':'POST',body);draft.value=null;await load();ElMessage.success('同步任务已保存')})}
async function execute(p:Profile){await runAction(async()=>{const job=await props.api(`/sync/profiles/${p.id}/run`,'POST',{});ElMessage.success(`“${p.name}”已加入队列（任务 #${job.id}）`)})}
async function remove(p:Profile){try{await ElMessageBox.confirm(`删除同步任务“${p.name}”？已入队的任务不会取消，云盘与本地文件不会被删除。`,'删除同步任务',{type:'warning'})}catch{return}await runAction(async()=>{await props.api(`/sync/profiles/${p.id}`,'DELETE');await load();ElMessage.success('同步任务已删除')})}
function timing(p:Profile){return !p.enabled?'定时关闭':`${p.frequency==='daily'?'每天':weekdays[p.weekday]} ${p.time}`}
</script>
<template>
  <el-alert v-if="error" :title="error" type="error" show-icon/>
  <p class="hint">每个任务保存一个目录、同步方式和定时。点击“立即执行”，直接按该任务的设置运行。</p>
  <div v-for="p in profiles" :key="p.id" class="directory-group">
    <div class="execution-row"><div><strong>{{p.name}}</strong><p>{{p.source_label}} · {{p.mode==='incremental'?'增量同步':'全量重建'}} · {{timing(p)}}</p><p v-if="p.enabled&&p.next_due">下次：{{new Date(p.next_due*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}}</p></div><el-button type="primary" :disabled="busy" @click="execute(p)">立即执行</el-button><el-button :disabled="busy" @click="draft={...p}">编辑</el-button><el-button text type="danger" :disabled="busy" @click="remove(p)">删除</el-button></div>
  </div>
  <p v-if="!profiles.length" class="hint">还没有同步任务。可以分别创建“影视库增量”“AV 全量”等任务。</p>
  <div class="button-row"><el-button type="primary" plain :disabled="busy" @click="draft=blank()">新增同步任务</el-button><el-button text @click="emit('navigate','tasks')">查看任务进度 →</el-button></div>
  <el-dialog :model-value="!!draft" :title="draft?.id?'编辑同步任务':'新增同步任务'" width="600px" @close="draft=null">
    <el-form v-if="draft" label-position="top">
      <el-form-item label="任务名称"><el-input v-model="draft.name" maxlength="80" placeholder="例如：AV 每天增量"/></el-form-item>
      <el-form-item label="同步目录"><div class="button-row"><el-button @click="picker=true">选择 115 目录</el-button><span>{{draft.source_label||'尚未选择'}}</span></div></el-form-item>
      <el-form-item label="同步方式"><el-radio-group v-model="draft.mode"><el-radio-button value="incremental">增量同步</el-radio-button><el-radio-button value="full">全量重建</el-radio-button></el-radio-group><p class="hint">增量适合日常新增；全量会重新逐项核对。</p></el-form-item>
      <el-form-item label="定时执行"><el-switch v-model="draft.enabled"/><span class="hint">{{draft.enabled?'已开启':'已关闭，仍可手动执行'}}</span></el-form-item>
      <template v-if="draft.enabled"><el-form-item label="频率"><el-radio-group v-model="draft.frequency"><el-radio-button value="daily">每天</el-radio-button><el-radio-button value="weekly">每周</el-radio-button></el-radio-group></el-form-item><el-form-item v-if="draft.frequency==='weekly'" label="星期"><el-select v-model="draft.weekday"><el-option v-for="(day,i) in weekdays" :key="i" :label="day" :value="i"/></el-select></el-form-item><el-form-item label="北京时间"><el-input v-model="draft.time" type="time" style="max-width:180px"/></el-form-item></template>
    </el-form>
    <template #footer><el-button @click="draft=null">取消</el-button><el-button type="primary" :disabled="busy" @click="save">保存任务</el-button></template>
  </el-dialog>
  <DirectoryPicker v-if="picker" :api="api" @select="select" @close="picker=false"/>
</template>
