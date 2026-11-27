<script setup lang="ts">
import PageHeader from './PageHeader.vue'
import {computed,onMounted,ref} from 'vue'
import {ArrowDown,Refresh} from '@element-plus/icons-vue'
import {useVisiblePolling} from './pageActivity'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
type CheckStatus = 'ok'|'warn'|'error'
type HealthCheck = {key:string;title:string;status:CheckStatus;message:string;detail:string;action:string;can_verify?:boolean}
type HealthSnapshot = {overall:CheckStatus;generated_at:number;checks:HealthCheck[]}
const data=ref<HealthSnapshot|null>(null),loading=ref(false),error=ref('')
const verifying=ref(false)
async function verifyBackup() {
  verifying.value=true
  try { await props.api('/system/backup/verify','POST'); await load() }
  catch(e) { error.value=(e as Error).message }
  finally { verifying.value=false }
}
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
const setupOrder=[
  {key:'cloud115',title:'登录 115',action:'account'},
  {key:'emby',title:'连接 Emby',action:'embyproxy'},
  {key:'tmdb',title:'配置 TMDB',action:'settings'},
  {key:'worker',title:'启动后台 Worker',action:'logs'},
  {key:'disk_data',title:'检查数据目录',action:'settings'},
  {key:'backup',title:'准备备份',action:'settings'},
]
const setupSteps=computed(()=>{
  const checks=new Map((data.value?.checks||[]).map(check=>[check.key,check]))
  return setupOrder.flatMap(step=>{
    const check=checks.get(step.key)
    if(!check||check.status==='ok')return []
    return [{key:step.key,title:step.title,action:check.action||step.action,message:check.message,detail:check.detail}]
  })
})
</script>
<template>
<PageHeader title="系统健康">
  <template #actions><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button></template>
</PageHeader>
<el-alert v-if="error" :title="error" type="error" :closable="false" class="global-error"/>
<section v-if="setupSteps.length" class="panel setup-panel">
  <div class="setup-heading"><div><h2>首次配置检查</h2><p>按顺序处理，完成后这里会自动消失。</p></div><el-tag type="warning">{{setupSteps.length}} 项待处理</el-tag></div>
  <ol class="setup-list"><li v-for="step in setupSteps" :key="step.key"><div><strong>{{step.title}}</strong><p>{{step.message}}</p><small v-if="step.detail">{{step.detail}}</small></div><el-button v-if="step.action" size="small" @click="emit('navigate',step.action)">去处理</el-button></li></ol>
</section>
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
      <el-button v-if="check.can_verify" :loading="verifying" size="small" @click="verifyBackup">校验备份</el-button>
      <el-button v-else-if="check.action" link type="primary" :aria-label="'处理'+check.title"
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
.setup-panel{margin-bottom:14px;padding:16px 18px}
.setup-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
.setup-heading h2{margin:0;font-size:16px}.setup-heading p{margin:5px 0 0;color:var(--muted);font-size:12px}
.setup-list{display:grid;gap:8px;margin:14px 0 0;padding:0;list-style:none}
.setup-list li{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--surface-soft)}
.setup-list li>div{min-width:0}.setup-list strong{font-size:13px}.setup-list p{margin:3px 0 0;font-size:12px;overflow-wrap:anywhere}.setup-list small{display:block;margin-top:3px;color:var(--muted);font-size:11px;overflow-wrap:anywhere}
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
