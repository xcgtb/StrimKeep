<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useVisiblePolling } from './pageActivity'

interface Settings {
  title: string
  subtitle: string
  layout: string
  color: string
  posters: number
  enabled: boolean
  daily_at: string
}
interface Status {
  state: string
  message: string
  outcome?: string
  last?: { applied_at?: number }
}
interface Library {
  id: string
  name: string
  settings: Settings
  status: Status
}
interface Layout {
  id: string
  name: string
  max_posters: number
}
interface BatchItem extends Status {
  id: string
  name: string
  command_id?: number
}
type BatchProgress = Status & { command_id: number }

const props = defineProps<{
  api: (path: string, method?: string, body?: unknown) => Promise<any>
  connectionRevision: number
}>()
const prefix = '/emby/library-covers'
const libraries = ref<Library[]>([])
const layouts = ref<Layout[]>([])
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
const batchOpen = ref(false)
const batchIds = ref<string[]>([])
const batchLayout = ref('saved')
const batchStarting = ref(false)
const batchError = ref('')
const batchItems = ref<BatchItem[]>([])
const selected = computed(() => libraries.value.find(library => library.id === selectedId.value))
const posterLimit = computed(() => layouts.value.find(layout => layout.id === draft.value?.layout)?.max_posters ?? 9)
const endpoint = computed(() => prefix + '/' + encodeURIComponent(selectedId.value))
const query = computed(() => '?scope=' + encodeURIComponent(scope.value))
const currentUrl = computed(() => '/api/v1' + endpoint.value + '/current' + query.value + '&v=' + refreshKey.value)
const pending = computed(() => ['queued', 'running'].includes(status.value?.state || ''))
const batchPending = computed(() => batchItems.value.filter(row => row.command_id && ['queued', 'running'].includes(row.state)))
const batchUpdated = computed(() => batchItems.value.filter(row => row.outcome === 'updated').length)
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
      if (scope.value !== data.scope) {
        batchItems.value = []
        batchIds.value = []
      }
      libraries.value = data.items
      layouts.value = data.layouts
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

function openBatch() {
  batchError.value = ''
  if (!batchItems.value.length) batchIds.value = selectedId.value ? [selectedId.value] : []
  batchOpen.value = true
}

async function startBatch() {
  const server = scope.value
  batchStarting.value = true
  batchError.value = ''
  try {
    const result = await props.api(prefix + '/batch', 'POST', {
      scope: server, library_ids: batchIds.value, layout: batchLayout.value === 'saved' ? null : batchLayout.value,
    })
    if (server === scope.value) batchItems.value = result.items
  } catch (failure) { batchError.value = (failure as Error).message }
  finally { batchStarting.value = false }
}

async function pollBatch() {
  if (!batchPending.value.length) return
  const items = batchItems.value
  const ids = batchPending.value.map(row => row.command_id).join(',')
  try {
    const result = await props.api(prefix + '/batch-status' + query.value + '&command_ids=' + ids)
    if (items !== batchItems.value) return
    const byId = new Map<number, BatchProgress>(result.items.map((row: BatchProgress) => [row.command_id, row]))
    batchItems.value = items.map(row => ({ ...row, ...byId.get(row.command_id ?? -1) }))
    batchError.value = ''
  } catch (failure) { batchError.value = (failure as Error).message }
}

function batchStatus(row: BatchItem) {
  if (row.state === 'done') return row.outcome === 'unchanged' ? '无变化' : '已更新'
  return ({ queued: '排队中', running: '生成并写入中', failed: '失败', skipped: '已跳过' })[row.state] || row.state
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
  await Promise.allSettled([pollBatch(), !busy.value && selected.value ? loadStatus() : undefined])
}, 4000)
</script>

<template>
  <section id="library-covers" class="panel settings-panel library-covers">
    <header class="cover-header">
      <div>
        <h2>媒体库封面</h2>
        <p class="hint">设置 Emby「我的媒体」里每个库的横版封面。</p>
      </div>
      <div class="cover-actions">
        <el-button :loading="busy === 'load'" :disabled="!!busy && busy !== 'load'" @click="load">刷新媒体库</el-button>
        <el-button v-if="libraries.length" type="primary" plain @click="openBatch">{{ batchPending.length ? `批量处理中（${batchPending.length}）` : '批量生成' }}</el-button>
      </div>
    </header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <p v-if="!libraries.length && !busy && !error" class="hint">Emby 还没有可用的媒体库。</p>
    <template v-if="draft">
      <el-select v-model="selectedId" aria-label="媒体库" :disabled="!!busy" class="cover-select">
        <el-option v-for="library in libraries" :key="library.id" :label="library.name" :value="library.id" />
      </el-select>
      <fieldset class="cover-presets" :disabled="!!busy">
        <legend>封面样式</legend>
        <div class="cover-preset-grid">
          <label v-for="layout in layouts" :key="layout.id" class="cover-preset" :class="{selected: draft.layout === layout.id}">
            <input v-model="draft.layout" type="radio" name="library-cover-layout" :value="layout.id" @change="invalidatePreview">
            <span class="cover-sample" :data-layout="layout.id" :style="{ '--sample-accent': draft.color }" aria-hidden="true">
              <i v-for="part in 4" :key="part" />
            </span>
            <span>{{ layout.name }}</span>
          </label>
        </div>
      </fieldset>
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
        <el-form-item label="主色"><input v-model="draft.color" type="color" aria-label="主色" :disabled="!!busy" @input="invalidatePreview"></el-form-item>
        <el-form-item v-if="posterLimit > 3" label="海报张数">
          <el-input-number :model-value="Math.min(draft.posters, posterLimit)" :min="3" :max="posterLimit"
            :value-on-clear="Math.min(6, posterLimit)" @update:model-value="draft.posters = $event || 6" @change="invalidatePreview" />
        </el-form-item>
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
    <el-dialog v-model="batchOpen" title="批量生成媒体库封面" width="min(580px, calc(100vw - 24px))" append-to-body>
      <div class="cover-batch">
        <p class="hint">生成后会自动替换所选媒体库的封面。</p>
        <el-form label-position="top">
          <el-form-item label="媒体库">
            <el-select v-model="batchIds" multiple collapse-tags collapse-tags-tooltip :multiple-limit="100"
              placeholder="选择要更新的媒体库" :disabled="batchStarting || !!batchPending.length">
              <el-option v-for="library in libraries" :key="library.id" :label="library.name" :value="library.id" />
            </el-select>
            <div class="cover-batch-select-actions">
              <el-button v-if="libraries.length <= 100" text :disabled="batchStarting || !!batchPending.length" @click="batchIds = libraries.map(library => library.id)">全选</el-button>
              <el-button text :disabled="batchStarting || !!batchPending.length || !batchIds.length" @click="batchIds = []">清空</el-button>
            </div>
          </el-form-item>
          <el-form-item label="套用样式">
            <el-select v-model="batchLayout" :disabled="batchStarting || !!batchPending.length">
              <el-option label="沿用各库已保存样式" value="saved" />
              <el-option v-for="layout in layouts" :key="layout.id" :label="layout.name" :value="layout.id" />
            </el-select>
          </el-form-item>
        </el-form>
        <el-alert v-if="batchError" :title="batchError" type="error" :closable="false" />
        <div v-if="batchItems.length" class="cover-batch-result" aria-live="polite">
          <strong>已处理 {{ batchItems.length - batchPending.length }} / {{ batchItems.length }} 个库 · 更新 {{ batchUpdated }} 个</strong>
          <ul>
            <li v-for="row in batchItems" :key="row.id">
              <span>{{ row.name }}</span><strong :class="{ 'cover-batch-failed': row.state === 'failed' }">{{ batchStatus(row) }}</strong>
              <small v-if="['skipped', 'failed'].includes(row.state) && row.message">{{ row.message }}</small>
            </li>
          </ul>
        </div>
      </div>
      <template #footer>
        <el-button @click="batchOpen = false">关闭</el-button>
        <el-button type="primary" :loading="batchStarting" :disabled="!batchIds.length || !!batchPending.length" @click="startBatch">生成并写入 Emby</el-button>
      </template>
    </el-dialog>
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
.cover-presets { margin: 0; padding: 0; min-inline-size: 0; border: 0 }
.cover-presets legend { padding: 0; margin-bottom: 7px; font-size: 13px; font-weight: 600 }
.cover-preset-grid { display: grid; grid-template-columns: repeat(8, minmax(0, 1fr)); gap: 8px }
.cover-preset { min-width: 0; position: relative; isolation: isolate; padding: 5px; display: grid; gap: 5px; justify-items: stretch; text-align: center; font-size: 12px; line-height: 1.3; cursor: pointer; border: 1px solid var(--el-border-color); border-radius: 8px }
.cover-preset.selected { background: var(--el-color-primary-light-9); border-color: var(--el-color-primary); color: var(--el-color-primary) }
.cover-preset:has(input:focus-visible) { outline: 2px solid var(--el-color-primary); outline-offset: 2px }
.cover-preset input { position: absolute; z-index: 1; opacity: 0; inset: 0; margin: 0; width: 100%; height: 100%; cursor: inherit }
.cover-presets:disabled .cover-preset { cursor: default }
.cover-sample { aspect-ratio: 16 / 9; display: block; position: relative; overflow: hidden; border-radius: 4px; background: linear-gradient(120deg, #171e30, color-mix(in srgb, var(--sample-accent) 65%, #171e30)) }
.cover-sample::after { content: ''; position: absolute; top: 46%; left: 10%; width: 28%; height: 7%; border-radius: 2px; background: #f5f2e9; box-shadow: 0 .35em 0 -.03em #d6e3ed8c }
.cover-sample i { --left: 51%; --top: 19%; display: none; position: absolute; left: var(--left); top: var(--top); width: var(--w, 17%); height: var(--h, 33%); border-radius: 2px; background: linear-gradient(155deg, #d9b188, #8e4563 55%, #342c55) }
.cover-sample i:nth-child(2n) { background: linear-gradient(145deg, #c8d4bc, #608291 50%, #333b60) }
.cover-sample i:nth-child(3n) { background: linear-gradient(155deg, #d5b18d, #626371 65%, #302d50) }
.cover-sample:is([data-layout="grid"], [data-layout="collage"], [data-layout="filmstrip"], [data-layout="magazine"]) i,
.cover-sample:is([data-layout="hero"], [data-layout="panorama"]) i:first-child,
.cover-sample[data-layout="stack"] i:nth-child(-n+3) { display: block }
.cover-sample:is([data-layout="grid"], [data-layout="collage"]) i:nth-child(even) { --left: 72% }
.cover-sample:is([data-layout="grid"], [data-layout="collage"]) i:nth-child(n+3) { --top: 56% }
.cover-sample[data-layout="collage"] i { transform: rotate(12deg); --w: 20% }
.cover-sample[data-layout="hero"] i { --w: 37%; --h: 95%; --left: 54%; --top: 3% }
.cover-sample[data-layout="filmstrip"] i { --w: 20%; --h: 43%; --top: 12%; left: 8% }
.cover-sample[data-layout="filmstrip"] i:nth-child(2) { left: 29% }
.cover-sample[data-layout="filmstrip"] i:nth-child(3) { left: 50% }
.cover-sample[data-layout="filmstrip"] i:nth-child(4) { left: 71% }
.cover-sample[data-layout="filmstrip"]::after { top: 69%; }
.cover-sample[data-layout="stack"] i { --w: 23%; --h: 61%; --top: 22%; --left: 48%; transform: rotate(-14deg) }
.cover-sample[data-layout="stack"] i:nth-child(2) { --left: 73%; transform: rotate(14deg) }
.cover-sample[data-layout="stack"] i:nth-child(3) { --left: 60%; --top: 15%; transform: none }
.cover-sample[data-layout="magazine"] i:first-child { --left: 47%; --top: 12%; --w: 24%; --h: 76% }
.cover-sample[data-layout="magazine"] i:nth-child(n+2) { --left: 74%; --top: 12%; --w: 14%; --h: 22% }
.cover-sample[data-layout="magazine"] i:nth-child(3) { --top: 39% }
.cover-sample[data-layout="magazine"] i:nth-child(4) { --top: 66% }
.cover-sample[data-layout="panorama"] i { --left: 0; --top: 0; --w: 100%; --h: 100%; background: radial-gradient(ellipse at 70% 13%, #b89a7c, #736278 48%, #20283c 82%) }
.cover-sample[data-layout="panorama"]::after { top: 71%; }
.cover-sample[data-layout="minimal"]::after { left: 36%; }
.cover-actions .el-button { margin: 0 }
.cover-schedule { border-top: 1px solid var(--el-border-color); padding-top: 12px }
.cover-schedule > div { flex: 1 1 230px }
.cover-time { display: grid; gap: 3px; font-size: 12px; color: var(--el-text-color-secondary) }
.cover-batch .hint { margin: 0 0 14px }
.cover-batch .el-select { width: 100% }
.cover-batch-select-actions .el-button { margin: 0; padding-inline: 6px }
.cover-batch-result { margin-top: 12px }
.cover-batch-result ul { padding: 0; margin: 8px 0 0; list-style: none; max-height: 250px; overflow-y: auto }
.cover-batch-result li { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 12px; padding: 8px 0; border-top: 1px solid var(--el-border-color-lighter) }
.cover-batch-result small { width: 100%; color: var(--el-text-color-secondary) }
.cover-batch-failed { color: var(--el-color-danger) }
@media (max-width: 1024px) {
  .cover-preset-grid { grid-template-columns: repeat(4, minmax(0, 1fr)) }
}
@media (max-width: 620px) {
  .cover-images { grid-template-columns: minmax(0, 1fr) }
}
</style>
