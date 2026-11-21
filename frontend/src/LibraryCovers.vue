<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useVisiblePolling } from './pageActivity'

interface Settings {
  title: string
  subtitle: string
  layout: 'collage' | 'grid'
  color: string
  posters: number
  enabled: boolean
  daily_at: string
}
interface Status {
  state: string
  message: string
  last?: { applied_at?: number }
}
interface Library {
  id: string
  name: string
  settings: Settings
  status: Status
}

const props = defineProps<{
  api: (path: string, method?: string, body?: unknown) => Promise<any>
  connectionRevision: number
}>()
const prefix = '/emby/library-covers'
const libraries = ref<Library[]>([])
const selectedId = ref('')
const scope = ref('')
const timezone = ref('')
const draft = ref<Settings>()
const status = ref<Status>()
const preview = ref('')
const posterCount = ref<number>()
const currentMissing = ref(false)
const refreshKey = ref(0)
const busy = ref('')
const error = ref('')
const notice = ref('')
const uploader = ref<HTMLInputElement>()
const selected = computed(() => libraries.value.find(library => library.id === selectedId.value))
const endpoint = computed(() => prefix + '/' + encodeURIComponent(selectedId.value))
const query = computed(() => '?scope=' + encodeURIComponent(scope.value))
const currentUrl = computed(() => '/api/v1' + endpoint.value + '/current' + query.value + '&v=' + refreshKey.value)
const pending = computed(() => ['queued', 'running'].includes(status.value?.state || ''))
const appliedAt = computed(() => status.value?.last?.applied_at
  ? new Date(status.value.last.applied_at * 1000).toLocaleString() : '')

function assignLibrary() {
  draft.value = selected.value ? { ...selected.value.settings } : undefined
  status.value = selected.value?.status
  preview.value = ''
  posterCount.value = undefined
  currentMissing.value = false
  refreshKey.value++
  error.value = ''
  notice.value = ''
}

async function run(action: string, perform: () => Promise<void>) {
  if (busy.value) return
  busy.value = action
  notice.value = ''
  error.value = ''
  try { await perform() }
  catch (failure) { error.value = (failure as Error).message }
  finally { busy.value = '' }
}

async function load() {
  await run('load', async () => {
    try {
      const data = await props.api(prefix)
      libraries.value = data.items
      scope.value = data.scope
      timezone.value = data.timezone
      if (!selected.value) selectedId.value = libraries.value[0]?.id || ''
      assignLibrary()
    } catch (failure) {
      libraries.value = []
      selectedId.value = ''
      assignLibrary()
      throw failure
    }
  })
}

function invalidatePreview() {
  preview.value = ''
  posterCount.value = undefined
}

function generate() {
  return run('preview', async () => {
    if (!draft.value) return
    const result = await props.api(endpoint.value + '/preview', 'POST', { ...draft.value, scope: scope.value })
    preview.value = result.image
    posterCount.value = result.poster_count
  })
}

async function previewUpload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  await run('upload', async () => {
    if (file.size > 2 * 1024 * 1024) throw new Error('请选择 2 MB 以内的图片。')
    const image = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '')
      reader.onerror = () => reject(new Error('无法读取图片。'))
      reader.readAsDataURL(file)
    })
    const result = await props.api(endpoint.value + '/upload-preview', 'POST', { scope: scope.value, image })
    preview.value = result.image
    posterCount.value = undefined
  })
}

function save() {
  return run('save', async () => {
    if (!draft.value || !selected.value) return
    const result = await props.api(endpoint.value + '/settings', 'PUT', { ...draft.value, scope: scope.value })
    selected.value.settings = result.settings
    draft.value = { ...result.settings }
    notice.value = '设置已保存。'
  })
}

function apply() {
  return run('apply', async () => {
    if (!preview.value || pending.value) return
    const result = await props.api(endpoint.value + '/apply', 'POST', { scope: scope.value, image: preview.value })
    status.value = { ...status.value, state: result.state, message: result.message }
    if (result.state === 'done') await loadStatus()
  })
}

async function loadStatus() {
  const library = selected.value
  const server = scope.value
  const previous = status.value
  if (!library) return
  const result: Status = await props.api(endpoint.value + '/status' + query.value)
  if (selected.value !== library || scope.value !== server || status.value !== previous) return
  if (previous?.last?.applied_at !== result.last?.applied_at || previous?.state !== result.state) {
    currentMissing.value = false
    refreshKey.value++
  }
  library.status = result
  status.value = result
}

onMounted(load)
watch(() => props.connectionRevision, load)
watch(selectedId, assignLibrary, { flush: 'sync' })
useVisiblePolling(async () => {
  if (!busy.value && selected.value) await loadStatus()
}, 4000)
</script>

<template>
  <section id="library-covers" class="panel settings-panel library-covers">
    <header class="cover-header">
      <div>
        <h2>媒体库封面</h2>
        <p class="hint">设置 Emby「我的媒体」里每个库的横版封面。</p>
      </div>
      <el-button :loading="busy === 'load'" :disabled="!!busy && busy !== 'load'" @click="load">刷新媒体库</el-button>
    </header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <p v-if="!libraries.length && !busy && !error" class="hint">Emby 还没有可用的媒体库。</p>
    <template v-if="draft">
      <el-select v-model="selectedId" aria-label="媒体库" :disabled="!!busy" class="cover-select">
        <el-option v-for="library in libraries" :key="library.id" :label="library.name" :value="library.id" />
      </el-select>
      <div class="cover-images">
        <figure>
          <div class="cover-frame">
            <img v-if="!currentMissing" :key="currentUrl" :src="currentUrl" alt="Emby 当前媒体库封面" @error="currentMissing = true">
            <span v-else>暂无可显示的封面</span>
          </div>
          <figcaption>当前封面</figcaption>
        </figure>
        <figure>
          <div class="cover-frame">
            <img v-if="preview" :src="'data:image/jpeg;base64,' + preview" alt="待应用的媒体库封面">
            <span v-else>生成预览或上传图片</span>
          </div>
          <figcaption>预览<span v-if="posterCount !== undefined"> · {{ posterCount }} 张作品海报</span></figcaption>
        </figure>
      </div>
      <el-form class="cover-fields" label-position="top" :disabled="!!busy">
        <el-form-item label="封面标题"><el-input v-model="draft.title" maxlength="48" :placeholder="selected?.name" @input="invalidatePreview" /></el-form-item>
        <el-form-item label="副标题"><el-input v-model="draft.subtitle" maxlength="30" placeholder="默认随媒体库类型" @input="invalidatePreview" /></el-form-item>
        <el-form-item label="排版">
          <el-select v-model="draft.layout" @change="invalidatePreview">
            <el-option label="斜向拼贴" value="collage" />
            <el-option label="整齐网格" value="grid" />
          </el-select>
        </el-form-item>
        <el-form-item label="主色"><input v-model="draft.color" type="color" aria-label="主色" :disabled="!!busy" @input="invalidatePreview"></el-form-item>
        <el-form-item label="海报张数"><el-input-number v-model="draft.posters" :min="3" :max="9" :value-on-clear="6" @change="invalidatePreview" /></el-form-item>
      </el-form>
      <div class="cover-actions">
        <el-button :loading="busy === 'preview'" :disabled="!!busy" @click="generate">生成预览</el-button>
        <el-button :loading="busy === 'upload'" :disabled="!!busy" @click="uploader?.click()">上传图片</el-button>
        <input ref="uploader" type="file" accept="image/jpeg,image/png,image/webp" hidden @change="previewUpload">
        <el-button type="primary" :loading="busy === 'apply' || pending" :disabled="!!busy || pending || !preview" @click="apply">应用到 Emby</el-button>
      </div>
      <p v-if="status?.message || appliedAt" class="hint cover-status" role="status">
        {{ status?.message }}<span v-if="appliedAt"> · 上次应用 {{ appliedAt }}</span>
      </p>
      <div class="cover-schedule">
        <div>
          <div class="cover-schedule-label"><strong>定时更新</strong><el-switch v-model="draft.enabled" aria-label="定时更新" :disabled="!!busy" /></div>
          <p class="hint">按已保存的样式，选取该库最近入库的作品生成封面。</p>
        </div>
        <label v-if="draft.enabled" class="cover-time">
          <span>每天 · {{ timezone }}</span><input v-model="draft.daily_at" type="time" aria-label="每天更新时间" :disabled="!!busy">
        </label>
        <el-button :loading="busy === 'save'" :disabled="!!busy || !draft.daily_at" @click="save">保存设置</el-button>
      </div>
      <p v-if="notice" class="hint cover-status" role="status">{{ notice }}</p>
    </template>
  </section>
</template>

<style scoped>
.library-covers { display: grid; gap: 14px; scroll-margin-top: 80px }
.cover-header, .cover-schedule, .cover-actions, .cover-schedule-label { display: flex; align-items: center; gap: 10px; flex-wrap: wrap }
.cover-header, .cover-schedule { justify-content: space-between }
.cover-header .hint, .cover-schedule .hint, .cover-status { margin: 4px 0 0 }
.cover-select { width: min(100%, 320px) }
.cover-images { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)) }
.cover-images figure { margin: 0; min-width: 0 }
.cover-frame { display: grid; place-items: center; aspect-ratio: 16 / 9; overflow: hidden; border: 1px solid var(--el-border-color); border-radius: 10px; color: var(--el-text-color-secondary); background: var(--el-fill-color-light); font-size: 13px }
.cover-frame img { display: block; width: 100%; height: 100%; object-fit: contain }
.cover-images figcaption { padding: 6px 0 0; font-size: 13px; color: var(--el-text-color-secondary) }
.cover-fields { display: grid; gap: 10px 14px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr)) }
.cover-fields :deep(.el-form-item) { margin: 0 }
.cover-fields :deep(.el-input-number) { width: 100% }
.cover-fields input[type="color"], .cover-time input { min-height: 32px; border: 1px solid var(--el-border-color); border-radius: var(--el-border-radius-base); background: var(--el-fill-color-blank); color: var(--el-text-color-primary); padding: 3px 8px }
.cover-fields input[type="color"] { width: 100%; cursor: pointer }
.cover-actions .el-button { margin: 0 }
.cover-schedule { border-top: 1px solid var(--el-border-color); padding-top: 12px }
.cover-schedule > div { flex: 1 1 230px }
.cover-time { display: grid; gap: 3px; font-size: 12px; color: var(--el-text-color-secondary) }
@media (max-width: 620px) {
  .cover-images { grid-template-columns: minmax(0, 1fr) }
}
</style>
