<script setup lang="ts">
import PageHeader from './PageHeader.vue'
import WorkspaceSections from './WorkspaceSections.vue'
import {ref,computed,watch} from 'vue'
import TmdbLookup from './TmdbLookup.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const section = ref<'lookup' | 'test' | 'aliases'>('lookup')
const text=ref('')
const folder=ref('')
const useTmdb=ref(true)
type Library = 'cloud' | 'share'
type RecognitionAlias = {
  id: number; alias: string; media_type: 'movie' | 'tv'; source_year: number
  tmdb_id: number; title: string; target_year: number
}
type Recognition = {
  name: string; ok: boolean; library: Library; stage?: string; reason?: string; note?: string
  hint?: { query?: string; media_type: 'tv' | 'movie'; season?: number; episode?: number; end_episode?: number; recognition_alias?: RecognitionAlias }
  work?: { id?: number; title?: string; year?: string }
  technical?: string; episode_title?: string; target_name?: string; parts?: string[]; category?: string
  classification?: {
    category_reason: string; region_reason: string; genres: { id: number; name: string }[]
  }
}
const library=ref<Library>('cloud')
const libraryNames: Record<Library, string> = {cloud:'网盘库',share:'分享库'}
const busy=ref(false),error=ref(''),rows=ref<Recognition[]>([])
const aliases=ref<RecognitionAlias[]>([])
const loadingAliases=ref(false),aliasError=ref(''),removingAlias=ref<number>()

const lines=computed(()=>text.value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean))
const needsReview = computed(() => rows.value.filter(row => !row.ok).length)

async function loadAliases() {
  if (loadingAliases.value) return
  loadingAliases.value=true
  aliasError.value=''
  try { aliases.value=(await props.api('/organize/recognition-aliases')).items }
  catch(e) { aliasError.value=(e as Error).message }
  finally { loadingAliases.value=false }
}

async function removeAlias(rule: RecognitionAlias) {
  if (removingAlias.value) return
  removingAlias.value=rule.id
  aliasError.value=''
  try {
    await props.api('/organize/recognition-aliases/'+rule.id,'DELETE')
    aliases.value=aliases.value.filter(alias=>alias.id!==rule.id)
  } catch(e) { aliasError.value=(e as Error).message }
  finally { removingAlias.value=undefined }
}

watch(section, value=>{if(value==='aliases')void loadAliases()})

async function test(){
  const names=lines.value
  if(!names.length){error.value='先粘贴至少一个文件名。';return}
  if(names.length>50){error.value='一次最多测试 50 个文件名。';return}
  const options = {use_tmdb:useTmdb.value, library:library.value, folder:folder.value.trim() || undefined}
  busy.value=true;error.value='';rows.value=[]
  try{
    let items: ({name:string}&typeof options)[] = []
    for (const name of names) {
      const item = {name,...options}
      if (items.length && new Blob([JSON.stringify({items:[...items,item]})]).size > 60000) {
        rows.value.push(...(await props.api('/organize/preview/batch','POST',{items})).items)
        items = []
      }
      items.push(item)
    }
    rows.value.push(...(await props.api('/organize/preview/batch','POST',{items})).items)
  }catch(e){error.value=(e as Error).message}finally{busy.value=false}
}

function work(row:Recognition){
  if(row.work?.title)return `${row.work.title}${row.work.year?' ('+row.work.year+')':''}`
  return row.hint?.query||row.name
}
function episode(row:Recognition){
  const h=row.hint
  if(!h||h.media_type!=='tv'||h.season==null||h.episode==null)return ''
  const code=`S${String(h.season).padStart(2,'0')}E${String(h.episode).padStart(2,'0')}`
  const end=h.end_episode&&h.end_episode!==h.episode?`-E${String(h.end_episode).padStart(2,'0')}`:''
  return code+end+(row.episode_title?` · ${row.episode_title}`:'')
}
function genreLabels(row:Recognition) {
  return row.classification?.genres.map(tag => tag.name ? `${tag.name} (${tag.id})` : String(tag.id)).join('、')
}
</script>

<template>
<PageHeader title="识别测试" description="查询 TMDB、预览归档结果和管理识别纠正。"/>
<WorkspaceSections v-model="section" label="识别工具" :items="[
  {id:'lookup', label:'TMDB 查询'}, {id:'test', label:'文件名测试'},
  {id:'aliases', label:'识别纠正'},
]"/>
<div v-show="section==='lookup'"><TmdbLookup :api="api"/></div>
<section v-if="section==='aliases'" class="panel recognize-results">
  <div class="section-title">
    <h2>已保存的纠正</h2><el-button :loading="loadingAliases" :disabled="!!removingAlias" @click="loadAliases">刷新</el-button>
  </div>
  <p class="hint">网盘库和分享库按解析出的作品名、类型和年份精确匹配。</p>
  <el-alert v-if="aliasError" :title="aliasError" type="error" :closable="false"/>
  <el-empty v-else-if="!loadingAliases && !aliases.length" description="在任务中心手动匹配时，可选择记住纠正。" :image-size="52"/>
  <div v-if="aliases.length" class="recognize-list">
    <article v-for="rule in aliases" :key="rule.id" class="recognize-result">
      <div class="result-heading">
        <h3>{{rule.alias}}</h3>
        <el-button type="danger" link :loading="removingAlias===rule.id" :disabled="!!removingAlias || loadingAliases" @click="removeAlias(rule)">移除</el-button>
      </div>
      <p class="result-meta">{{rule.media_type==='tv'?'剧集':'电影'}} · {{rule.source_year||'无识别年份'}}</p>
      <p class="result-path">→ {{rule.title}} ({{rule.target_year}}) · TMDB {{rule.tmdb_id}}</p>
    </article>
  </div>
</section>
<div v-show="section==='test'">
<section class="panel recognize-input">
  <div class="section-title"><h2>待测文件名</h2><span class="muted">{{lines.length}} 行</span></div>
  <el-input v-model="text" aria-label="待测文件名" type="textarea" :rows="5" placeholder="每行一个文件名，例如 繁花.S01E01.1080p.mp4"/>
  <el-form label-position="top" class="mt">
    <el-form-item label="父目录（可选）">
      <el-input v-model="folder" placeholder="例如 媒体库/剧集/欧美剧集/末日地堡 (2023) {tmdb-125988}/Season 1"/>
    </el-form-item>
    <el-form-item v-if="useTmdb" label="整理规则">
      <el-radio-group v-model="library" :disabled="busy">
        <el-radio-button value="cloud">网盘库</el-radio-button>
        <el-radio-button value="share">分享库</el-radio-button>
      </el-radio-group>
    </el-form-item>
  </el-form>
  <div class="button-row">
    <el-checkbox v-model="useTmdb" :disabled="busy">查询 TMDB 并预览分类</el-checkbox>
    <el-button type="primary" :loading="busy" :disabled="!lines.length" @click="test">开始识别</el-button>
  </div>
  <el-alert v-if="error" :title="error" type="error" :closable="false" class="mt"/>
</section>

<section v-if="rows.length" class="panel recognize-results">
  <div class="section-title">
    <h2>识别结果</h2>
    <span class="muted" role="status">{{rows.length}} 项<span v-if="needsReview"> · {{needsReview}} 项需核查</span></span>
  </div>
  <p v-if="rows.some(row => row.parts)" class="hint">目录按新作品预览，已有作品可能沿用原目录。</p>
  <div class="recognize-list">
    <article v-for="(row,index) in rows" :key="index" class="recognize-result">
      <div class="result-heading">
        <h3>{{work(row)}}</h3>
        <el-tag :type="!row.ok?'warning':row.category?'success':'info'" size="small">
          {{ !row.ok ? '需核查' : row.category || (row.hint?.recognition_alias ? '命中识别纠正' : '仅解析文件名') }}
        </el-tag>
      </div>
      <p v-if="episode(row) || row.work?.id" class="result-meta">
        <span v-if="episode(row)">{{ episode(row) }}</span>
        <span v-if="row.work?.id">TMDB {{ row.work.id }}</span>
      </p>
      <p v-if="!row.ok" class="result-reason">{{row.reason}}</p>
      <div v-if="row.parts?.length" class="result-path">
        <span>{{libraryNames[row.library]}}归档目录</span>
        <p>{{row.parts.join(' / ')}}</p>
      </div>
      <details :open="rows.length===1">
        <summary>{{ row.classification ? '分类与命名依据' : '文件名解析' }}</summary>
        <dl class="result-details">
          <template v-if="row.hint?.recognition_alias">
            <dt>识别纠正</dt><dd>{{row.hint.recognition_alias.alias}} → {{row.hint.recognition_alias.title}} · TMDB {{row.hint.recognition_alias.tmdb_id}}</dd>
          </template>
          <template v-if="row.classification">
            <dt>分类</dt><dd>{{row.classification.category_reason}}</dd>
            <template v-if="row.classification.region_reason"><dt>地区</dt><dd>{{row.classification.region_reason}}</dd></template>
            <dt>TMDB 类型</dt><dd>{{genreLabels(row) || '未提供'}}</dd>
          </template>
          <dt>源文件名</dt><dd>{{row.name}}</dd>
          <template v-if="row.target_name"><dt>预览文件名</dt><dd>{{row.target_name}}</dd></template>
          <template v-if="row.technical"><dt>文件名技术字段</dt><dd>{{row.technical}}</dd></template>
        </dl>
        <p v-if="row.note" class="hint">{{row.note}}</p>
      </details>
    </article>
  </div>
</section>
</div>
</template>

<style scoped>

.recognize-input :deep(.el-textarea__inner){min-height:140px;background:var(--surface-soft);padding:14px;line-height:1.8}
.recognize-list{display:grid;gap:10px}
.recognize-result{min-width:0;border:1px solid var(--line);border-radius:10px;padding:14px;overflow-wrap:anywhere}
.result-heading,.result-meta{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:6px 12px}
.result-heading h3{margin:0;font-size:15px}
.result-meta{justify-content:flex-start;margin:7px 0 0;color:var(--muted);font-size:12px}
.result-reason{font-size:13px;margin:8px 0 0}
.result-path{margin-top:10px;padding:8px 10px;border-radius:6px;background:var(--surface-soft);font-size:13px}
.result-path span{display:block;color:var(--muted);font-size:12px;margin-bottom:3px}
.result-path p{margin:0}
.recognize-result details{margin-top:10px;font-size:13px}
.recognize-result summary{cursor:pointer;color:var(--accent)}
.result-details{display:grid;grid-template-columns:auto minmax(0,1fr);gap:7px 12px;margin:10px 0 0}
.result-details dt{color:var(--muted)}
.result-details dd{margin:0}
@media(max-width:760px){.recognize-input .button-row{align-items:flex-start}.recognize-input .button-row :deep(.el-button){margin-left:auto}}
</style>
