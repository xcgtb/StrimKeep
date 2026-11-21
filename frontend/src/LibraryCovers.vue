<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useVisiblePolling } from './pageActivity'

interface Studio {
  layout: string
  color: string
  posters: number
  library_ids: string[]
  enabled: boolean
  daily_at: string
}
interface Library { id: string; name: string }
interface Layout { id: string; name: string; max_posters: number }
interface Progress {
  command_id: number
  state: string
  outcome?: string
  message?: string
}
interface BatchItem extends Library, Progress {}

const props = defineProps<{
  api: (path: string, method?: string, body?: unknown) => Promise<any>
  connectionRevision: number
}>()
const prefix = '/emby/library-covers'
const scope = ref('')
const timezone = ref('')
const libraries = ref<Library[]>([])
const layouts = ref<Layout[]>([])
const studio = ref<Studio>()
const sample = ref('')
const previews = ref<Record<string, string>>({})
const galleryOpen = ref(false)
const galleryBusy = ref(false)
const galleryError = ref('')
const busy = ref('')
const error = ref('')
const notice = ref('')
const batch = ref<BatchItem[]>([])
let previewRevision = 0

const selected = computed(() => layouts.value.find(layout => layout.id === studio.value?.layout))
const ready = computed(() => !!studio.value?.library_ids.length && !!studio.value?.daily_at && !busy.value)
const pending = computed(() => batch.value.filter(item => ['queued', 'running'].includes(item.state)))
const updated = computed(() => batch.value.filter(item => item.outcome === 'updated').length)
const sampleName = computed(() => libraries.value.find(library => library.id === sample.value)?.name || '')
const source = (data: string) => 'data:image/jpeg;base64,' + data

function style(settings: Studio) {
  return { layout: settings.layout, color: settings.color, posters: settings.posters }
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
    const data = await props.api(prefix)
    if (scope.value !== data.scope) batch.value = []
    scope.value = data.scope
    timezone.value = data.timezone
    libraries.value = data.items
    layouts.value = data.layouts
    studio.value = data.settings
    if (!libraries.value.some(library => library.id === sample.value)) sample.value = data.items[0]?.id || ''
    refreshPreview()
  })
}

async function fetchPreviews(ids: string[]) {
  const settings = studio.value
  if (!settings || !sample.value || !ids.length) return
  const revision = ++previewRevision
  galleryBusy.value = true
  galleryError.value = ''
  try {
    const result = await props.api(prefix + '/gallery', 'POST', {
      scope: scope.value, library_id: sample.value, ...style(settings), layouts: ids,
    })
    if (revision !== previewRevision) return
    previews.value = { ...previews.value, ...Object.fromEntries(
      result.items.map((item: { id: string; image: string }) => [item.id, item.image]),
    ) }
  } catch (failure) {
    if (revision === previewRevision) galleryError.value = (failure as Error).message
  } finally {
    if (revision === previewRevision) galleryBusy.value = false
  }
}

function refreshPreview() {
  ++previewRevision
  previews.value = {}
  galleryBusy.value = false
  galleryError.value = ''
  const ids = galleryOpen.value ? layouts.value.map(item => item.id) : [studio.value?.layout || 'collage']
  void fetchPreviews(ids)
}

function browse() {
  galleryOpen.value = true
  const missing = layouts.value.filter(item => !previews.value[item.id])
  void fetchPreviews(missing.map(item => item.id))
}

function choose(layout: Layout) {
  if (!studio.value || !previews.value[layout.id]) return
  studio.value.layout = layout.id
  galleryOpen.value = false
}

function save() {
  return run('save', async () => {
    if (!studio.value) return
    const result = await props.api(prefix + '/settings', 'PUT', { ...studio.value, scope: scope.value })
    studio.value = result.settings
    notice.value = '已保存统一封面设置。'
  })
}

function apply() {
  return run('apply', async () => {
    if (!studio.value || pending.value.length) return
    const result = await props.api(prefix + '/batch', 'POST', {
      ...studio.value, scope: scope.value,
    })
    batch.value = result.items
  })
}

async function pollBatch() {
  if (!pending.value.length) return
  const snapshot = batch.value
  const ids = pending.value.map(item => item.command_id).join(',')
  const data = await props.api(prefix + '/batch-status?scope=' + encodeURIComponent(scope.value) + '&command_ids=' + ids)
  if (snapshot !== batch.value) return
  const results = new Map<number, Progress>(data.items.map((item: Progress) => [item.command_id, item]))
  batch.value = snapshot.map(item => ({ ...item, ...results.get(item.command_id) }))
}

function describe(item: BatchItem) {
  if (item.state === 'done') return item.outcome === 'unchanged' ? '无变化' : '已更新'
  return ({ queued: '排队中', running: '生成中', failed: '失败', skipped: '已跳过' })[item.state] || item.state
}

onMounted(load)
watch(() => props.connectionRevision, load)
useVisiblePolling(pollBatch, 3000)
</script>

<template>
  <section id="library-covers" class="panel settings-panel cover-studio">
    <header class="studio-header">
      <div>
        <h2>媒体库封面</h2>
        <p class="hint">为 Emby「我的媒体」的多个库统一封面样式。</p>
      </div>
      <el-button text :loading="busy === 'load'" :disabled="!!busy" @click="load">刷新媒体库</el-button>
    </header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <p v-if="!libraries.length && !busy && !error" class="hint">Emby 暂无可用媒体库。</p>

    <template v-if="studio && libraries.length">
      <div class="cover-composition">
        <button type="button" class="featured-cover" @click="browse">
          <img v-if="selected && previews[selected.id]" :src="source(previews[selected.id])" alt="">
          <span v-else class="cover-empty">{{ galleryBusy ? '正在生成预览…' : '浏览封面样式' }}</span>
          <span class="featured-caption"><strong>{{ selected?.name }}</strong><span>浏览样式 ›</span></span>
        </button>
        <div class="studio-parameters">
          <label class="studio-field">
            <span>主色</span><input v-model="studio.color" type="color" @change="refreshPreview">
          </label>
          <label v-if="(selected?.max_posters || 0) > 3" class="studio-field">
            <span>海报张数</span>
            <el-input-number :model-value="Math.min(studio.posters, selected?.max_posters || 9)"
              :min="3" :max="selected?.max_posters || 9" :value-on-clear="6"
              @update:model-value="studio.posters = $event || 6" @change="refreshPreview" />
          </label>
        </div>
      </div>

      <div class="studio-selection">
        <div class="studio-line">
          <div><strong>更新媒体库</strong><span class="selection-count">已选 {{ studio.library_ids.length }} / {{ libraries.length }}</span></div>
          <div class="studio-buttons">
            <el-button v-if="libraries.length <= 100" text :disabled="!!busy" @click="studio.library_ids = libraries.map(item => item.id)">全选</el-button>
            <el-button text :disabled="!!busy || !studio.library_ids.length" @click="studio.library_ids = []">清空</el-button>
          </div>
        </div>
        <el-checkbox-group v-model="studio.library_ids" class="studio-libraries" :max="100" :disabled="!!busy" aria-label="要更新的媒体库">
          <el-checkbox v-for="library in libraries" :key="library.id" :label="library.name" :value="library.id" />
        </el-checkbox-group>
      </div>

      <div class="studio-line studio-footer">
        <div class="studio-schedule">
          <strong>定时更新</strong>
          <el-switch v-model="studio.enabled" aria-label="定时更新" :disabled="!!busy" />
          <label v-if="studio.enabled" class="studio-field"><input v-model="studio.daily_at" type="time" aria-label="每天更新时间">
            <span class="hint">{{ timezone }}</span></label>
        </div>
        <div class="studio-buttons">
          <el-button :loading="busy === 'save'" :disabled="!!busy || (studio.enabled && (!studio.daily_at || !studio.library_ids.length))" @click="save">保存设置</el-button>
          <el-button type="primary" :loading="busy === 'apply' || !!pending.length" :disabled="!ready || !!pending.length" @click="apply">批量应用到 Emby</el-button>
        </div>
      </div>
      <p v-if="notice" class="hint studio-notice" role="status">{{ notice }}</p>
      <div v-if="batch.length" class="studio-result" aria-live="polite">
        <strong>已处理 {{ batch.length - pending.length }} / {{ batch.length }} 个库 · 更新 {{ updated }} 个</strong>
        <ul>
          <li v-for="item in batch" :key="item.id">
            <span>{{ item.name }}</span><strong :class="{ failed: item.state === 'failed' }">{{ describe(item) }}</strong>
            <small v-if="['skipped', 'failed'].includes(item.state) && item.message">{{ item.message }}</small>
          </li>
        </ul>
      </div>
    </template>

    <el-dialog v-model="galleryOpen" title="选择封面样式" width="min(940px, calc(100vw - 24px))" top="5vh" append-to-body>
      <div class="cover-gallery">
        <div class="studio-line gallery-toolbar">
          <span class="hint">从媒体库取图预览</span>
          <el-select v-model="sample" aria-label="预览媒体库" :placeholder="sampleName" @change="refreshPreview">
            <el-option v-for="library in libraries" :key="library.id" :label="library.name" :value="library.id" />
          </el-select>
        </div>
        <el-alert v-if="galleryError" :title="galleryError" type="error" :closable="false">
          <el-button text @click="refreshPreview">重试</el-button>
        </el-alert>
        <p v-if="galleryBusy" class="hint" role="status">正在生成样式预览…</p>
        <div class="gallery-tiles">
          <button v-for="layout in layouts" :key="layout.id" type="button" class="gallery-tile"
            :disabled="!previews[layout.id]" :aria-pressed="studio?.layout === layout.id" @click="choose(layout)">
            <img v-if="previews[layout.id]" :src="source(previews[layout.id])" alt="" loading="lazy">
            <span v-else class="cover-empty" aria-hidden="true" />
            <span class="gallery-label">{{ layout.name }}<small v-if="studio?.layout === layout.id">当前</small></span>
          </button>
        </div>
      </div>
    </el-dialog>
  </section>
</template>

<style scoped>
.cover-studio { display: grid; gap: 16px; scroll-margin-top: 80px }
.studio-header, .studio-line, .studio-buttons, .studio-schedule, .studio-field { display: flex; align-items: center; gap: 8px; flex-wrap: wrap }
.studio-header, .studio-line { justify-content: space-between }
.studio-header .hint, .studio-notice { margin: 3px 0 0 }
.studio-buttons .el-button { margin: 0 }
.cover-composition { display: grid; grid-template-columns: minmax(0, 1fr) minmax(144px, 190px); gap: 14px; align-items: start }
.featured-cover, .gallery-tile { min-width: 0; text-align: left; color: inherit; background: var(--el-fill-color-blank); border: 1px solid var(--el-border-color); padding: 0; overflow: hidden; border-radius: 10px; cursor: pointer }
.featured-cover { max-width: 600px; width: 100% }
.featured-cover img, .gallery-tile img, .cover-empty { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover; background: var(--el-fill-color-light) }
.cover-empty { display: grid; place-items: center; color: var(--el-text-color-secondary); font-size: 13px }
.featured-caption, .gallery-label { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 9px 12px }
.featured-caption > span, .gallery-label small { color: var(--el-color-primary); font-size: 12px }
.studio-parameters { display: grid; gap: 10px; padding-top: 3px }
.studio-field { font-size: 13px; color: var(--el-text-color-regular) }
.studio-field input { min-height: 34px; border: 1px solid var(--el-border-color); border-radius: 6px; background: var(--el-fill-color-blank); color: var(--el-text-color-primary); padding: 3px 7px }
.studio-field input[type=color] { width: 44px; cursor: pointer }
.studio-field .hint { margin: 0 }
.studio-selection, .studio-footer, .studio-result { padding-top: 12px; border-top: 1px solid var(--el-border-color-lighter) }
.selection-count { margin-left: 9px; font-size: 12px; color: var(--el-text-color-secondary) }
.studio-libraries { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 140px), 1fr)); gap: 6px; max-height: 194px; overflow: auto; padding: 2px; margin-top: 4px }
.studio-libraries .el-checkbox { min-width: 0; margin: 0; background: var(--el-fill-color-light); border-radius: 6px; padding: 0 9px }
.studio-libraries .el-checkbox :deep(.el-checkbox__label) { overflow: hidden; text-overflow: ellipsis }
.studio-result ul { list-style: none; margin: 6px 0 0; padding: 0; max-height: 250px; overflow: auto }
.studio-result li { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 3px 12px; padding: 6px 0; border-top: 1px solid var(--el-border-color-lighter) }
.studio-result small { width: 100%; color: var(--el-text-color-secondary) }
.studio-result .failed { color: var(--el-color-danger) }
.cover-gallery { max-height: 75vh; max-height: 75dvh; overflow-y: auto; overscroll-behavior: contain; padding: 2px 3px }
.gallery-toolbar { margin-bottom: 12px }
.gallery-toolbar .el-select { width: min(100%, 210px) }
.gallery-tiles { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px }
.gallery-tile:hover, .gallery-tile[aria-pressed=true] { border-color: var(--el-color-primary) }
.gallery-tile:disabled { cursor: default }
.gallery-label { font-size: 14px }
@media (max-width: 640px) {
  .cover-composition, .gallery-tiles { grid-template-columns: minmax(0, 1fr) }
  .studio-parameters { display: flex; flex-wrap: wrap; justify-content: space-between }
  .studio-footer > .studio-buttons { margin-left: auto }
}
</style>
