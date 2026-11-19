<script setup lang="ts">
import {computed,onMounted,ref} from 'vue'
import {useVisiblePolling} from './pageActivity'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const data=ref<any>(null),loading=ref(false),error=ref('')
async function load(){if(loading.value)return;loading.value=true;error.value='';try{data.value=await props.api('/system/health')}catch(e){error.value=(e as Error).message}finally{loading.value=false}}
onMounted(load)
useVisiblePolling(load,30000)
const overallLabel=computed(()=>data.value?.overall==='error'?'存在异常':data.value?.overall==='warn'?'需要关注':'总体正常')
const overallType=computed(()=>data.value?.overall==='error'?'danger':data.value?.overall==='warn'?'warning':'success')
function mark(status:string){return status==='error'?'×':status==='warn'?'!':'✓'}
function when(ts:number){return ts?new Date(ts*1000).toLocaleString('zh-CN',{hour12:false}):''}
</script>
<template>
<div class="page-title"><div><h1>系统健康</h1><p class="muted">像 Sonarr Health Checks 一样，把数据库、Worker、115、Emby、TMDB、磁盘、备份和失败项汇总到一页。</p></div><el-button :loading="loading" @click="load">刷新</el-button></div>
<el-alert v-if="error" :title="error" type="error" :closable="false"/>
<section v-if="data" class="panel health-panel">
 <div class="health-summary"><el-tag :type="overallType" size="large">{{overallLabel}}</el-tag><span class="muted">检查时间 {{when(data.generated_at)}} · 每 30 秒自动刷新</span></div>
 <div class="health-list">
  <article v-for="check in data.checks" :key="check.key" class="health-row" :class="'is-'+check.status">
   <span class="health-mark" aria-hidden="true">{{mark(check.status)}}</span>
   <div class="health-copy"><strong>{{check.title}}</strong><p>{{check.message}}</p><small v-if="check.detail">{{check.detail}}</small></div>
   <el-button v-if="check.action" link type="primary" @click="emit('navigate',check.action)">去处理</el-button>
  </article>
 </div>
</section>
</template>
<style scoped>
.health-panel{display:grid;gap:18px}.health-summary{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.health-list{display:grid;gap:10px}.health-row{display:flex;align-items:flex-start;gap:12px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--el-bg-color)}.health-row.is-warn{border-color:var(--el-color-warning-light-5);background:var(--el-color-warning-light-9)}.health-row.is-error{border-color:var(--el-color-danger-light-5);background:var(--el-color-danger-light-9)}.health-mark{display:grid;place-items:center;width:24px;height:24px;border-radius:50%;font-weight:700;color:#fff;background:var(--el-color-success);flex:none}.is-warn .health-mark{background:var(--el-color-warning)}.is-error .health-mark{background:var(--el-color-danger)}.health-copy{flex:1;min-width:0}.health-copy strong{font-size:14px}.health-copy p{margin:4px 0 0;font-size:13px;overflow-wrap:anywhere}.health-copy small{display:block;margin-top:4px;color:var(--el-text-color-secondary);font-size:12px;overflow-wrap:anywhere}
@media(max-width:639px){.health-row{padding:10px 12px}.health-row :deep(.el-button){margin-left:0}}
</style>
