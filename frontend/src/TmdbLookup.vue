<script setup lang="ts">
import {ref} from 'vue'
import {ElMessage} from 'element-plus'
import {copyText} from './clipboard'
import {suggestedDirectory} from './tmdbLookup'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
type Item={id:number;title:string;year:string;overview:string;media_type:'movie'|'tv'}
const query=ref(''),filter=ref<'all'|'movie'|'tv'>('all'),busy=ref(false),searched=ref(false),results=ref<Item[]>([]),error=ref(''),searchedQuery=ref('')
async function search(){
  const q=query.value.trim().replace(/(?<=[a-zA-Z])\.(?=[a-zA-Z])/g,' ')
  if(!q){error.value='请输入电影或剧集名称。';return}
  if(q.length>200){error.value='搜索内容不能超过 200 个字符。';return}
  const types=filter.value==='all'?['movie','tv']:[filter.value]
  busy.value=true;error.value='';results.value=[];searched.value=false;searchedQuery.value=q
  try{
    const responses=await Promise.allSettled(types.map(media_type=>props.api('/tmdb/search?'+new URLSearchParams({q,media_type}))))
    const failures:string[]=[]
    const groups:Item[][]=[]
    responses.forEach((response,i)=>{
      if(response.status==='fulfilled')groups.push(response.value.items||[])
      else failures.push(`${types[i]==='movie'?'电影':'剧集'}：${(response.reason as Error).message||'查询失败'}`)
    })
    for(let i=0;i<Math.max(0,...groups.map(g=>g.length));i++)for(const group of groups)if(group[i])results.value.push(group[i])
    if(failures.length)error.value=failures.join('；')
    searched.value=responses.some(r=>r.status==='fulfilled')
  }finally{busy.value=false}
}
async function copy(value:string){try{await copyText(value,navigator.clipboard,document);ElMessage.success('已复制')}catch{ElMessage.error('复制失败，请手动选择文字复制')}}
</script>
<template>
<section class="panel lookup-panel">
  <div class="section-title"><div><h2>TMDB 人工查询</h2><p class="lookup-intro">搜索作品，核对正确版本后复制目录命名或 ID。使用系统设置中保存的 TMDB Key。</p></div></div>
  <div class="lookup-form"><el-input v-model="query" maxlength="200" clearable placeholder="输入电影或剧名，例如 征途" aria-label="作品名称" @keyup.enter="search"/><el-button type="primary" :loading="busy" @click="search">搜索 TMDB</el-button></div>
  <div class="lookup-filter"><span>搜索范围</span><el-radio-group v-model="filter" :disabled="busy" aria-label="搜索范围"><el-radio-button value="all">全部</el-radio-button><el-radio-button value="movie">电影</el-radio-button><el-radio-button value="tv">剧集</el-radio-button></el-radio-group></div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="lookup-error"/>
  <div v-if="searched" class="lookup-summary">“{{searchedQuery}}” · {{results.length}} 个结果<span v-if="results.length">，各类型最多显示 20 条</span></div>
  <el-empty v-if="searched&&!results.length" description="没有找到匹配作品，可换个名称再试" :image-size="70"/>
  <div v-if="results.length" class="lookup-results">
    <article v-for="item in results" :key="item.media_type+':'+item.id" class="lookup-card">
      <div class="lookup-title"><span class="lookup-kind">{{item.media_type==='tv'?'剧集':'电影'}}</span><strong>{{item.title||'未提供名称'}}</strong><span>{{item.year||'年份未知'}}</span></div>
      <p v-if="item.overview" class="lookup-overview">{{item.overview}}</p>
      <div class="lookup-id"><span>TMDB ID</span><code>{{item.id}}</code><el-button text type="primary" @click="copy(String(item.id))">复制 ID</el-button><a :href="`https://www.themoviedb.org/${item.media_type}/${item.id}`" target="_blank" rel="noopener noreferrer">打开 TMDB ↗</a></div>
      <div class="lookup-name"><span>目录命名建议</span><code v-if="suggestedDirectory(item)">{{suggestedDirectory(item)}}</code><span v-else class="lookup-unavailable">名称或年份不完整，暂不能生成</span><el-button v-if="suggestedDirectory(item)" size="small" @click="copy(suggestedDirectory(item))">复制命名</el-button></div>
    </article>
  </div>
  <p class="lookup-note">建议用于作品目录；剧集文件仍需保留季集信息。查询和复制不会修改网盘或本地文件。</p>
</section>
</template>
<style scoped>
.lookup-intro,.lookup-note{color:var(--muted);font-size:13px;line-height:1.6;margin:7px 0 0}.lookup-form{display:flex;gap:10px;margin-top:18px}.lookup-form :deep(.el-input){max-width:680px}.lookup-filter{display:flex;gap:14px;align-items:center;margin-top:15px;color:var(--muted);font-size:13px}.lookup-error{margin-top:16px}.lookup-summary{margin:20px 0 12px;color:var(--muted);font-size:13px}.lookup-results{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,390px),1fr));gap:12px}.lookup-card{min-width:0;padding:16px;border:1px solid var(--line);border-radius:10px;background:var(--surface-soft)}.lookup-title{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;color:var(--muted)}.lookup-title strong{font-size:15px;color:var(--text);overflow-wrap:anywhere}.lookup-kind{padding:3px 8px;background:var(--el-color-primary-light-9);color:var(--el-color-primary);border-radius:6px}.lookup-overview{color:var(--muted);font-size:12px;line-height:1.6;margin:10px 0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.lookup-id{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px;font-size:12px;color:var(--muted)}.lookup-id a{color:var(--el-color-primary);text-decoration:none}.lookup-id :deep(.el-button){padding:2px 4px;height:auto}.lookup-name{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px;padding-top:12px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}.lookup-name code{color:var(--text);overflow-wrap:anywhere;flex:1;min-width:140px}.lookup-unavailable{flex:1}.lookup-note{margin-top:16px}@media(max-width:600px){.lookup-form{align-items:stretch}.lookup-form :deep(.el-button){padding:0 12px}.lookup-card{padding:14px}.lookup-name code{flex-basis:100%}}
</style>
