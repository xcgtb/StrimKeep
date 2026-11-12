<script setup lang="ts">
import {ref,computed} from 'vue'
import {ElMessage} from 'element-plus'
import TmdbLookup from './TmdbLookup.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const text=ref('')
const folder=ref('')
const useTmdb=ref(true)
const busy=ref(false),error=ref(''),rows=ref<any[]>([])

const lines=computed(()=>text.value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean))

async function test(){
  const names=lines.value
  if(!names.length){error.value='先粘贴至少一个文件名。';return}
  if(names.length>50){error.value='一次最多测试 50 个文件名。';return}
  busy.value=true;error.value='';rows.value=[]
  try{
    for(const name of names){
      const body:any={name,use_tmdb:useTmdb.value}
      if(folder.value.trim())body.folder=folder.value.trim()
      try{rows.value.push(await props.api('/organize/preview','POST',body))}
      catch(e){rows.value.push({name,ok:false,reason:(e as Error).message})}
    }
    const bad=rows.value.filter(r=>!r.ok).length
    ElMessage.success(bad?`测试完成，${rows.value.length-bad} 个可识别，${bad} 个需核查`:`${rows.value.length} 个文件名全部识别成功`)
  }catch(e){error.value=(e as Error).message}finally{busy.value=false}
}

function kind(row:any){
  if(!row.hint)return '—'
  return row.hint.media_type==='tv'?'剧集':'电影'
}
function work(row:any){
  if(row.work)return `${row.work.title||'—'}${row.work.year?' ('+row.work.year+')':''}`
  return row.hint?.query||'—'
}
function episode(row:any){
  const h=row.hint
  if(!h||h.media_type!=='tv')return '—'
  const code=`S${String(h.season).padStart(2,'0')}E${String(h.episode).padStart(2,'0')}`
  const end=h.end_episode&&h.end_episode!==h.episode?`-E${String(h.end_episode).padStart(2,'0')}`:''
  return code+end+(row.episode_title?` · ${row.episode_title}`:'')
}
function note(row:any){
  if(!row.ok)return row.reason||'未识别'
  if(row.stage==='parsed')return '只做了文件名解析（未查 TMDB）'
  if(row.reason)return `TMDB 未匹配：${row.reason}`
  return '可自动整理'
}
</script>

<template>
<div class="page-heading"><div><h1>识别测试</h1><p class="muted">手动查询作品或测试文件名识别；查询不会修改网盘和本地文件。</p></div></div>
<TmdbLookup :api="api"/>
<details class="advanced-recognize"><summary>高级工具：文件名识别测试</summary><p class="hint">粘贴文件名，预览整理时的识别与命名结果。</p>
<section class="panel recognize-input">
  <div class="section-title"><h2>待测文件名</h2><span class="muted">{{lines.length}} 行</span></div>
  <el-input v-model="text" type="textarea" :rows="5" placeholder="每行粘贴一个文件名，例如：繁花.S01E01.1080p.mp4"/>
  <el-form label-position="top" class="mt">
    <el-form-item label="父目录（可选）">
      <el-input v-model="folder" placeholder="例如 媒体库/剧集/欧美剧集/末日地堡 (2023) {tmdb-125988}/Season 1"/>
    </el-form-item>
  </el-form>
  <div class="button-row">
    <el-checkbox v-model="useTmdb">同时查询 TMDB，给出最终文件名</el-checkbox>
    <el-button type="primary" :loading="busy" @click="test">开始识别</el-button>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" class="mt"/>
</section>

<section v-if="rows.length" class="panel recognize-results">
  <div class="section-title"><h2>识别结果</h2><span class="muted">技术字段来自文件名；真实整理以 ffprobe 探测为准</span></div>
  <p class="hint mobile-table-hint">左右滑动表格可查看完整结果。</p>
  <el-table :data="rows" size="small" style="width:100%">
    <el-table-column label="输入" min-width="220"><template #default="{row}"><span class="mono">{{row.name}}</span></template></el-table-column>
    <el-table-column label="类型" width="70"><template #default="{row}">{{kind(row)}}</template></el-table-column>
    <el-table-column label="作品" min-width="150"><template #default="{row}">{{work(row)}}</template></el-table-column>
    <el-table-column label="季集" min-width="150"><template #default="{row}">{{episode(row)}}</template></el-table-column>
    <el-table-column label="技术字段" min-width="200"><template #default="{row}"><span class="mono">{{row.technical||'—'}}</span></template></el-table-column>
    <el-table-column label="最终文件名" min-width="260"><template #default="{row}"><span class="mono">{{row.target_name||'—'}}</span></template></el-table-column>
    <el-table-column label="说明" min-width="160"><template #default="{row}"><el-tag :type="row.ok?'success':'warning'" size="small">{{note(row)}}</el-tag></template></el-table-column>
  </el-table>
</section>
</details>
</template>

<style scoped>

.advanced-recognize{margin-top:16px}.advanced-recognize>summary{cursor:pointer;color:var(--el-color-primary);font-weight:600;padding:12px 4px}.advanced-recognize>.hint{margin:0 4px 12px}.recognize-input :deep(.el-textarea__inner){min-height:140px;background:var(--surface-soft);padding:14px;line-height:1.8}.recognize-results :deep(.el-table){border:1px solid var(--line);border-radius:8px;overflow:hidden}.recognize-results :deep(.el-table__cell){padding:11px 0}.recognize-results .mono{overflow-wrap:anywhere}.mobile-table-hint{display:none}
@media(max-width:760px){.recognize-input .button-row{align-items:flex-start}.recognize-input .button-row :deep(.el-button){margin-left:auto}.recognize-results{padding:14px 10px!important}.mobile-table-hint{display:block;margin:0 0 10px;font-size:12px}}
</style>
