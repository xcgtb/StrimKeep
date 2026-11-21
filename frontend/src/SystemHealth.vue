<script setup lang="ts">
import PageHeader from './PageHeader.vue'
import {computed,onMounted,ref} from 'vue'
import {ArrowDown,Refresh} from '@element-plus/icons-vue'
import {useVisiblePolling} from './pageActivity'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
type CheckStatus = 'ok'|'warn'|'error'
type HealthCheck = {key:string;title:string;status:CheckStatus;message:string;detail:string;action:string}
type HealthSnapshot = {overall:CheckStatus;generated_at:number;checks:HealthCheck[]}
const data=ref<HealthSnapshot|null>(null),loading=ref(false),error=ref('')
async function load(){if(loading.value)return;loading.value=true;error.value='';try{data.value=await props.api('/system/health')}catch(e){error.value=(e as Error).message}finally{loading.value=false}}
onMounted(load)
useVisiblePolling(load,30000)
const overallLabels={ok:'运行正常',warn:'需要关注',error:'存在异常'}
const attention=computed(()=>[
  ...(data.value?.checks.filter(check=>check.status==='error')||[]),
  ...(data.value?.checks.filter(check=>check.status==='warn')||[]),
])
const healthy=computed(()=>data.value?.checks.filter(check=>check.status==='ok')||[])
const checkedAt=computed(()=>data.value ? new Date(data.value.generated_at*1000) : null)
</script>
<template>
<PageHeader title="系统健康">
  <template #actions><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button></template>
</PageHeader>
<el-alert v-if="error" :title="error" type="error" :closable="false" class="global-error"/>
<el-skeleton v-if="!data&&loading" :rows="2" animated/>
<section v-if="data" class="panel health-panel" :aria-busy="loading">
  <div class="health-summary">
    <h2 class="health-overall" :class="'is-'+data.overall">
      <span class="health-dot" aria-hidden="true"></span>{{overallLabels[data.overall]}}
    </h2>
    <time v-if="checkedAt" :datetime="checkedAt.toISOString()">上次检查 {{checkedAt.toLocaleTimeString('zh-CN',{hour12:false})}}</time>
  </div>
  <ul v-if="attention.length" class="health-list">
    <li v-for="check in attention" :key="check.key" class="health-row" :class="'is-'+check.status">
      <div class="health-copy">
        <strong>{{check.title}}</strong>
        <p>{{check.message}}</p>
        <small v-if="check.detail">{{check.detail}}</small>
      </div>
      <el-button v-if="check.action" link type="primary" :aria-label="'处理'+check.title"
        @click="emit('navigate',check.action)">去处理</el-button>
    </li>
  </ul>
  <details v-if="healthy.length" class="health-details">
    <summary>
      <span>正常检查项 <span class="health-count">{{healthy.length}}</span></span>
      <el-icon aria-hidden="true"><ArrowDown/></el-icon>
    </summary>
    <dl>
      <div v-for="check in healthy" :key="check.key">
        <dt>{{check.title}}</dt>
        <dd>{{check.detail||check.message}}</dd>
      </div>
    </dl>
  </details>
</section>
</template>
<style scoped>
.health-panel{padding:0 18px}
.health-summary,.health-details summary{display:flex;align-items:center;justify-content:space-between;gap:8px 16px}
.health-summary{flex-wrap:wrap;padding:16px 0}
.health-summary .health-overall{display:flex;align-items:center;gap:8px;margin:0;font-size:15px}
.health-summary time,.health-count{color:var(--muted);font-size:12px}
.health-dot{width:9px;height:9px;border-radius:50%;background:var(--el-color-success)}
.is-warn .health-dot{background:var(--el-color-warning)}
.is-error .health-dot{background:var(--el-color-danger)}
.health-list{display:grid;gap:8px;list-style:none;padding:0;margin:0 0 12px}
.health-row{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:7px;border-inline-start:3px solid}
.health-row.is-warn{border-color:var(--el-color-warning);background:var(--el-color-warning-light-9)}
.health-row.is-error{border-color:var(--el-color-danger);background:var(--el-color-danger-light-9)}
.health-copy{min-width:0;flex:1}
.health-copy strong,.health-details summary{font-size:14px}
.health-copy p{margin:3px 0 0;font-size:13px;overflow-wrap:anywhere}
.health-copy small{display:block;margin-top:3px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.health-details{border-top:1px solid var(--line)}
.health-details summary{list-style:none;cursor:pointer;padding:13px 0}
.health-details summary::-webkit-details-marker{display:none}
.health-details summary .el-icon{color:var(--muted)}
.health-details[open] summary .el-icon{transform:rotate(180deg)}
.health-count{margin-left:4px}
.health-details dl{margin:0 0 6px}
.health-details dl>div{display:grid;grid-template-columns:minmax(78px,1fr) minmax(0,3fr);gap:12px;padding:8px 0;border-top:1px solid var(--line)}
.health-details dt,.health-details dd{font-size:13px;margin:0;overflow-wrap:anywhere}
.health-details dd{color:var(--muted);text-align:right}
@media(max-width:760px){.health-panel{padding:0 14px!important}.health-summary time{font-size:11px}}
</style>
