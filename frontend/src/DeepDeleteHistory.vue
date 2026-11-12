<script setup lang="ts">
import {onMounted,ref,shallowRef} from 'vue'
import {useVisiblePolling} from './pageActivity'

type Detail={path:string;state:string;actions:string[]}
type GroupItem={path:string;state:string;message:string}
type Group={key:string;kind:string;work:string;title:string;season:string;total:number;done:number;skipped:number;failed:number;items:GroupItem[];items_total:number}
type Rollup={works:number;seasons:number;files:number;groups:number}
type Batch={batch:string;at:number;mode:string;source:string;total:number;done:number;skipped:number;failed:number;queued:number;details:Detail[];groups?:Group[];rollup?:Rollup}
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const batches=shallowRef<Batch[]>([]),loading=ref(false),error=ref(''),loaded=ref(false)
async function load(){
 if(loading.value)return
 loading.value=true
 try{const result=await props.api('/emby/deep-delete/history?limit=20');batches.value=Array.isArray(result.batches)?result.batches:[];error.value='';loaded.value=true}
 catch(e){error.value=(e as Error).message||'读取深度删除记录失败'}finally{loading.value=false}
}
const status=(state:string)=>({queued:'排队中',done:'完成',skipped:'跳过',failed:'失败'} as Record<string,string>)[state]||state
const groupKind=(g:Group)=>({season:'剧集',share:'分享',single:'电影'} as Record<string,string>)[g.kind]||'条目'
const groupTitle=(g:Group)=>g.kind==='season'?(g.title+' · '+g.season):g.title
const stamp=(at:number)=>at?new Date(at*1000).toLocaleString('zh-CN',{hour12:false}):'时间未知'
onMounted(()=>{void load()});useVisiblePolling(load,10000)
</script>
<template>
 <div class="deep-history-heading"><div><h2>深度删除记录</h2><p class="hint">最近 20 批，包含预演和实际执行；未命中的事件可在实时日志查看。剧集按季折叠，电影/分享单条列出。</p></div><el-button :loading="loading" @click="load">刷新</el-button></div>
 <el-alert v-if="error" :title="error" type="error" show-icon :closable="false"/>
 <el-empty v-if="loaded&&!batches.length" description="暂无深度删除记录" :image-size="64"/>
 <article v-for="batch in batches" :key="batch.batch" class="deep-batch">
  <div class="deep-batch-heading"><div><strong>{{stamp(batch.at)}}</strong><span>{{batch.mode==='预演'?'预演':'实际执行'}}</span></div><el-tag :type="batch.failed?'danger':batch.queued?'warning':'success'" size="small">{{batch.queued?'处理中':batch.failed?'有失败':'已处理'}}</el-tag></div>
  <p v-if="batch.source" class="deep-source">{{batch.source}}</p>
  <div class="deep-counts"><span>总计 {{batch.total}}</span><span>完成 {{batch.done}}</span><span>跳过 {{batch.skipped}}</span><span>失败 {{batch.failed}}</span><span v-if="batch.queued">排队 {{batch.queued}}</span><span v-if="batch.rollup" class="deep-rollup">{{batch.rollup.works}} 部作品 · {{batch.rollup.seasons}} 季 · {{batch.rollup.files}} 个文件</span></div>
  <div v-if="batch.groups&&batch.groups.length" class="deep-groups">
   <details v-for="group in batch.groups" :key="group.key" class="deep-group">
    <summary class="deep-group-summary"><span class="deep-kind">{{groupKind(group)}}</span><strong>{{groupTitle(group)}}</strong><span class="deep-group-count">共 {{group.total}} · 完成 {{group.done}} · 跳过 {{group.skipped}}<template v-if="group.failed"> · 失败 {{group.failed}}</template></span></summary>
    <div class="deep-group-items">
     <div v-for="(item,index) in group.items" :key="index" class="deep-detail"><span :class="['deep-state',item.state]">{{status(item.state)}}</span><div><strong>{{item.path||'路径未知'}}</strong><p v-if="item.message">{{item.message}}</p></div></div>
     <p v-if="group.items_total>group.items.length" class="hint">其余 {{group.items_total-group.items.length}} 条明细略。</p>
    </div>
   </details>
  </div>
  <details v-else class="deep-details"><summary>查看明细（{{batch.details.length}}{{batch.total>batch.details.length?' / '+batch.total:''}}）</summary>
   <p v-if="batch.total>batch.details.length" class="hint">每批最多显示前 100 条明细。</p>
   <div v-for="(item,index) in batch.details" :key="index" class="deep-detail"><span :class="['deep-state',item.state]">{{status(item.state)}}</span><div><strong>{{item.path||'路径未知'}}</strong><p v-for="(action,i) in item.actions" :key="i">{{action}}</p></div></div>
  </details>
 </article>
</template>
<style scoped>
.deep-history-heading,.deep-batch-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.deep-history-heading h2{margin:0}.deep-history-heading .hint{margin:6px 0 18px}.deep-history-heading :deep(.el-button){flex:none}.deep-batch{padding:16px;margin:12px 0;border:1px solid var(--line);border-radius:9px;background:var(--surface-soft)}.deep-batch-heading>div{display:flex;align-items:center;gap:9px;flex-wrap:wrap;min-width:0}.deep-batch-heading strong{font-size:13px}.deep-batch-heading span{color:var(--muted);font-size:12px}.deep-source{margin:10px 0;color:var(--text);font-size:13px;line-height:1.6;overflow-wrap:anywhere}.deep-counts{display:flex;flex-wrap:wrap;gap:6px 14px;color:var(--muted);font-size:12px;margin-top:10px}.deep-details{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}.deep-details summary{cursor:pointer;color:var(--accent);font-size:13px}.deep-detail{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--line);font-size:12px}.deep-detail:last-child{border:0}.deep-detail>div{min-width:0}.deep-detail strong,.deep-detail p{overflow-wrap:anywhere;line-height:1.6}.deep-detail p{margin:4px 0;color:var(--muted)}.deep-state{flex:none;color:var(--muted)}.deep-state.failed{color:var(--el-color-danger)}.deep-state.queued{color:var(--el-color-warning)}.deep-rollup{color:var(--accent)}.deep-groups{margin-top:12px;border-top:1px solid var(--line)}.deep-group{border-bottom:1px solid var(--line)}.deep-group:last-child{border-bottom:0}.deep-group-summary{display:flex;align-items:center;gap:10px;padding:10px 0;cursor:pointer;list-style:none;font-size:13px}.deep-group-summary::-webkit-details-marker{display:none}.deep-kind{flex:none;font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:1px 6px}.deep-group-count{margin-left:auto;color:var(--muted);font-size:12px}.deep-group-items{padding-left:8px}
@media(max-width:760px){.deep-batch{padding:13px}.deep-history-heading .hint{max-width:26ch}.deep-counts{gap:6px 10px}.deep-group-summary{flex-wrap:wrap}.deep-group-count{margin-left:0;width:100%}}
</style>
