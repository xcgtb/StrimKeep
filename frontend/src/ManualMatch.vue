<script setup lang="ts">
import {ref} from 'vue'
import {ElMessage} from 'element-plus'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>,jobId:number,item:any}>()
const emit=defineEmits<{close:[],created:[job:any]}>()
type Kind='movie'|'tv'
type Result={id:number;title:string;year:string;overview:string;media_type:Kind}
const query=ref(String(props.item?.original_name||'').replace(/\.[^.]+$/,'').replace(/[._]+/g,' ').trim())
const filter=ref<'all'|Kind>('all')
const tmdbId=ref<number|undefined>()
const mediaType=ref<'all'|Kind>('all')
const season=ref<number|undefined>(1)
const episode=ref<number|undefined>(1)
const selected=ref<Result|null>(null)
const results=ref<Result[]>([])
const busy=ref(false),searching=ref(false),error=ref('')
async function search(){
  const q=query.value.trim()
  if(!q){error.value='请输入作品名称。';return}
  if(q.length>200){error.value='搜索内容不能超过 200 个字符。';return}
  searching.value=true;error.value='';results.value=[]
  try{
    const types:Kind[]=filter.value==='all'?['movie','tv']:[filter.value]
    const responses=await Promise.allSettled(types.map(media_type=>props.api('/tmdb/search?'+new URLSearchParams({q,media_type}))))
    const groups:Result[][]=[]
    responses.forEach((response,i)=>{
      if(response.status==='fulfilled')groups.push(response.value.items||[])
      else error.value=`${types[i]==='movie'?'电影':'剧集'}查询失败：${(response.reason as Error).message||'未知错误'}`
    })
    for(let i=0;i<Math.max(0,...groups.map(group=>group.length));i++)for(const group of groups)if(group[i])results.value.push(group[i])
    if(!results.value.length&&!error.value)error.value='没有找到匹配作品，可换个名称再试。'
  }finally{searching.value=false}
}
function choose(item:Result){
  selected.value=item;tmdbId.value=item.id;mediaType.value=item.media_type
  if(item.media_type==='tv'){season.value=season.value??1;episode.value=episode.value??1}
  error.value=''
}
async function submit(){
  if(!tmdbId.value){error.value='请先搜索并选择作品，或直接填写 TMDB ID。';return}
  if(mediaType.value==='all'){error.value='请选择电影或剧集。';return}
  if(mediaType.value==='tv'&&(season.value==null||episode.value==null)){error.value='剧集需要填写季号和集号。';return}
  busy.value=true;error.value=''
  try{
    const result=await props.api(`/organize/jobs/${props.jobId}/items/${props.item.id}/manual-match`,'POST',{
      tmdb_id:tmdbId.value,media_type:mediaType.value,
      season:mediaType.value==='tv'?season.value:undefined,
      episode:mediaType.value==='tv'?episode.value:undefined,
    })
    ElMessage.success(`已创建手动匹配任务 #${result.job.id}`)
    emit('created',result.job);emit('close')
  }catch(e){error.value=(e as Error).message}finally{busy.value=false}
}
</script>

<template>
<el-dialog :model-value="true" title="手动匹配未识别文件" width="min(760px, 100%)" @close="emit('close')">
  <p class="hint manual-file">原文件：<span class="mono">{{item.original_name}}</span></p>
  <el-alert type="warning" :closable="false" show-icon title="会新建一个只处理此文件的整理任务；原任务记录保留，不会删除文件。手动匹配失败时原位保留，不再转入未识别目录。"/>
  <div class="manual-search">
    <el-input v-model="query" maxlength="200" clearable placeholder="作品名称，例如 黑客帝国" @keyup.enter="search"/>
    <el-radio-group v-model="filter" :disabled="searching" aria-label="搜索类型">
      <el-radio-button value="all">全部</el-radio-button>
      <el-radio-button value="movie">电影</el-radio-button>
      <el-radio-button value="tv">剧集</el-radio-button>
    </el-radio-group>
    <el-button type="primary" :loading="searching" @click="search">搜索 TMDB</el-button>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="manual-error"/>
  <div v-if="results.length" class="manual-results">
    <button v-for="result in results" :key="result.media_type+':'+result.id" type="button" class="manual-result" :class="{selected:selected?.id===result.id&&selected?.media_type===result.media_type}" @click="choose(result)">
      <span class="manual-kind">{{result.media_type==='tv'?'剧集':'电影'}}</span>
      <strong>{{result.title||'未提供名称'}}</strong>
      <span>{{result.year||'年份未知'}}</span>
      <code>{{result.id}}</code>
    </button>
  </div>
  <div class="manual-form">
    <label>TMDB ID<el-input-number v-model="tmdbId" :min="1" :max="2000000000" :controls="false" aria-label="TMDB ID"/></label>
    <label>类型<el-select v-model="mediaType" aria-label="作品类型"><el-option label="电影" value="movie"/><el-option label="剧集" value="tv"/></el-select></label>
    <template v-if="mediaType==='tv'">
      <label>季号<el-input-number v-model="season" :min="0" :max="1000" aria-label="季号"/></label>
      <label>集号<el-input-number v-model="episode" :min="1" :max="100000" aria-label="集号"/></label>
    </template>
  </div>
  <p v-if="selected" class="hint">已选择：{{selected.title}}（{{selected.year||'年份未知'}}）· TMDB {{selected.id}}</p>
  <template #footer>
    <el-button :disabled="busy" @click="emit('close')">取消</el-button>
    <el-button type="primary" :loading="busy" @click="submit">创建手动整理任务</el-button>
  </template>
</el-dialog>
</template>

<style scoped>
.manual-file{margin:0 0 12px;overflow-wrap:anywhere}.manual-search{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:16px 0}.manual-search :deep(.el-input){flex:1;min-width:220px}.manual-error{margin:12px 0}.manual-results{display:grid;gap:8px;max-height:260px;overflow:auto;padding:2px;margin:12px 0}.manual-result{display:flex;gap:9px;align-items:center;width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--surface-soft);color:var(--text);text-align:left;cursor:pointer}.manual-result:hover,.manual-result.selected{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 9%,var(--surface))}.manual-result strong{flex:1;min-width:0;overflow-wrap:anywhere}.manual-result code{color:var(--muted);font-size:12px}.manual-kind{padding:2px 7px;border-radius:5px;background:var(--surface);color:var(--muted);font-size:11px}.manual-form{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}.manual-form label{display:grid;gap:6px;font-size:12px;color:var(--muted)}.manual-form :deep(.el-input-number){width:150px}.manual-form :deep(.el-select){width:150px}@media(max-width:600px){.manual-search{align-items:stretch}.manual-search :deep(.el-button){width:100%}.manual-form label,.manual-form :deep(.el-input-number),.manual-form :deep(.el-select){width:100%}}
</style>
