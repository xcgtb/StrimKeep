<script setup lang="ts">
import PageHeader from './PageHeader.vue'
import WorkspaceSections from './WorkspaceSections.vue'
import { nextTick, onActivated, onMounted, ref, watch } from 'vue'
import { Connection, FolderOpened, Tickets } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'
import EmbyMapping from './EmbyMapping.vue'
import EmbyProxyServers from './EmbyProxyServers.vue'
import LibraryCovers from './LibraryCovers.vue'
import SettingCard from './SettingCard.vue'

const props = defineProps<{ api: (path: string, method?: string, body?: unknown) => Promise<any> }>()
const route = useRoute()
const section = ref<'proxy' | 'media' | 'covers'>(route.hash === '#media-info' ? 'media' : 'proxy')
const coversVisited = ref(false)
const mediaSection = ref<HTMLElement>()
const mediaCard = ref<InstanceType<typeof SettingCard>>()
const connectionBusy = ref('')
const connectionError = ref('')
const connections = ref({ emby_url: '', emby_key: '' })
const savedUrl = ref('')
const mainKeyConfigured = ref(false)
const connectionRevision = ref(0)
const mappingRevision = ref(0)
const media = ref<{ enabled: boolean; extract: boolean; cron_enabled: boolean; cron: string; timezone: string }>()
const mediaBusy = ref('')
const mediaError = ref('')
const diagnosticsBusy = ref(false)
const diagnosticsError = ref('')
const mediaStatus = ref<any>()
const mediaFailures = ref<any>()

function updateConnection(info: { emby_url: string; emby_key_configured: boolean }) {
  savedUrl.value = info.emby_url || ''
  mainKeyConfigured.value = info.emby_key_configured
  connections.value = { emby_url: savedUrl.value, emby_key: '' }
}

async function loadConnection() {
  connectionBusy.value = 'load'
  try { updateConnection(await props.api('/settings')) }
  catch (e) { connectionError.value = (e as Error).message }
  finally { connectionBusy.value = '' }
}

async function saveConnection() {
  connectionBusy.value = 'save'
  connectionError.value = ''
  try {
    const { emby_url, emby_key } = connections.value
    const response = await props.api('/settings', 'PUT', emby_key ? { emby_url, emby_key } : { emby_url })
    updateConnection(response)
    connectionRevision.value++
    ElMessage.success('主 Emby 连接已保存')
  } catch (e) { connectionError.value = (e as Error).message }
  finally { connectionBusy.value = '' }
}

async function checkConnection() {
  connectionBusy.value = 'test'
  connectionError.value = ''
  try {
    await props.api('/connections/emby/test', 'POST', connections.value)
    ElMessage.success('主 Emby 连接正常')
  } catch (e) { connectionError.value = (e as Error).message }
  finally { connectionBusy.value = '' }
}

async function loadMedia() {
  try { media.value = await props.api('/emby/media-info/config') }
  catch (e) { mediaError.value = (e as Error).message }
}

async function saveMedia() {
  if (!media.value) return
  mediaBusy.value = 'save'
  mediaError.value = ''
  try {
    const { enabled, extract, cron_enabled, cron } = media.value
    media.value = await props.api('/emby/media-info/config', 'PUT', { enabled, extract, cron_enabled, cron })
    ElMessage.success('媒体信息设置已保存')
  } catch (e) { mediaError.value = (e as Error).message }
  finally { mediaBusy.value = '' }
}

async function injectMedia() {
  mediaBusy.value = 'inject'
  mediaError.value = ''
  try {
    const result = await props.api('/emby/media-info/inject', 'POST')
    const errors = Number(result.failed || result.failures?.length || 0)
    if (errors) ElMessage.warning(`已写入 ${result.injected} 条，失败 ${errors} 条`)
    else ElMessage.success(result.injected ? `已写入 ${result.injected} 条` : '没有需要补写的条目')
  } catch (e) { mediaError.value = (e as Error).message }
  finally { mediaBusy.value = '' }
}

async function loadMediaStatus() {
  diagnosticsBusy.value = true
  diagnosticsError.value = ''
  try {
    const [status, failures] = await Promise.all([
      props.api('/emby/media-info/status'), props.api('/emby/media-info/failures?limit=200'),
    ])
    mediaStatus.value = status
    mediaFailures.value = failures
  } catch (e) { diagnosticsError.value = (e as Error).message }
  finally { diagnosticsBusy.value = false }
}

async function revealMedia() {
  if (route.name !== 'embyproxy' || route.hash !== '#media-info') return
  section.value = 'media'
  await nextTick()
  mediaCard.value?.expand()
  mediaSection.value?.scrollIntoView({ block: 'start' })
}

onMounted(() => {
  void loadConnection()
  void loadMedia()
})
onActivated(revealMedia)
watch(() => route.hash, revealMedia, { flush: 'post' })
watch(section, value => { if (value === 'covers') coversVisited.value = true })
</script>

<template>
  <PageHeader title="Emby 反代" description="管理播放实例、路径映射和媒体库设置。"/>
  <WorkspaceSections v-model="section" label="Emby 反代分区" :items="[
    {id:'proxy', label:'反代与连接'}, {id:'media', label:'媒体信息'}, {id:'covers', label:'媒体库封面'},
  ]"/>
  <div v-show="section === 'proxy'">
  <EmbyProxyServers :api="api" :revision="connectionRevision + mappingRevision" />

  <SettingCard title="主 Emby 连接" description="供入库通知、库存读取和媒体库封面使用。" :summary="savedUrl || undefined" :icon="Connection">
    <el-alert v-if="connectionError" :title="connectionError" type="error" :closable="false" />
    <el-form label-position="top" :disabled="!!connectionBusy" @submit.prevent="saveConnection">
      <el-form-item label="Emby 服务地址"><el-input v-model="connections.emby_url" placeholder="http://192.0.2.10:8096" /></el-form-item>
      <el-form-item label="Emby API Key">
        <el-input v-model="connections.emby_key" type="password" autocomplete="new-password" show-password
          :placeholder="mainKeyConfigured ? '已保存，留空保留原值' : '填写 API Key'" />
      </el-form-item>
    </el-form>
    <div class="emby-actions">
      <el-button :loading="connectionBusy === 'test'" :disabled="!!connectionBusy && connectionBusy !== 'test'" @click="checkConnection">检查连接</el-button>
      <el-button type="primary" :loading="connectionBusy === 'save'" :disabled="!!connectionBusy && connectionBusy !== 'save'" @click="saveConnection">保存连接</el-button>
    </div>
  </SettingCard>

  <SettingCard title="默认路径映射" description="供主 Emby 和沿用默认映射的反代使用。" :icon="FolderOpened">
    <EmbyMapping :api="api" @saved="mappingRevision++" />
  </SettingCard>
  </div>

  <div v-show="section === 'media'" id="media-info" ref="mediaSection">
    <SettingCard ref="mediaCard" title="媒体信息写入" :description="media ? (media.enabled ? '已启用' : '已关闭') : undefined"
      :icon="Tickets" default-open>
      <el-alert v-if="mediaError" :title="mediaError" type="error" :closable="false" />
      <div v-if="media" class="media-settings">
        <div class="workflow-switch-row"><strong>媒体信息处理</strong><el-switch v-model="media.enabled" :disabled="!!mediaBusy" aria-label="媒体信息处理" /></div>
        <div class="workflow-switch-row"><strong>新入库自动提取</strong><el-switch v-model="media.extract" :disabled="!media.enabled || !!mediaBusy" aria-label="新入库自动提取" /></div>
        <div class="workflow-switch-row"><strong>定时补写</strong><el-switch v-model="media.cron_enabled" :disabled="!media.enabled || !!mediaBusy" aria-label="定时补写" /></div>
        <el-form v-if="media.cron_enabled" class="media-schedule" label-position="top" :disabled="!media.enabled || !!mediaBusy">
          <el-form-item :label="'Cron · ' + media.timezone"><el-input v-model="media.cron" placeholder="0 4 * * *" /></el-form-item>
        </el-form>
        <div class="emby-actions">
          <el-button :loading="mediaBusy === 'inject'" :disabled="!media.enabled || (mediaBusy !== '' && mediaBusy !== 'inject')" @click="injectMedia">立即补写</el-button>
          <el-button type="primary" :loading="mediaBusy === 'save'" :disabled="!!mediaBusy && mediaBusy !== 'save'" @click="saveMedia">保存设置</el-button>
        </div>
      </div>
      <details class="emby-details">
        <summary>补写说明</summary>
        <p>先在 Emby 添加并扫描媒体库，再补写缺少视频流的 STRM 条目。补写需要挂载 Emby 数据目录并设置 <code>STRIMKEEP_EMBY_LIBRARY_DB</code>。</p>
      </details>
      <details class="emby-details">
        <summary>写入状态与失败记录</summary>
        <el-button :loading="diagnosticsBusy" @click="loadMediaStatus">读取状态</el-button>
        <el-alert v-if="diagnosticsError" :title="diagnosticsError" type="error" :closable="false" />
        <template v-if="mediaStatus">
          <p>数据库 {{ !mediaStatus.configured ? '未配置' : mediaStatus.exists ? '已找到' : '未找到' }} · 待写入 {{ mediaStatus.pending ?? '暂不可用' }} · 未修复 {{ mediaFailures?.count ?? 0 }}</p>
          <p v-if="mediaStatus.hint">{{ mediaStatus.hint }}</p>
          <details v-for="row in mediaFailures?.items || []" :key="row.item_id">
            <summary>{{ row.name || row.item_id }} · {{ row.reason_text }}</summary>
            <p>{{ row.path }}</p>
          </details>
        </template>
      </details>
    </SettingCard>
  </div>

  <LibraryCovers v-if="coversVisited" v-show="section === 'covers'" :api="api" :connection-revision="connectionRevision" />
</template>

<style scoped>
#media-info{scroll-margin-top:84px}
.emby-actions{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;margin-top:12px}
.emby-actions :deep(.el-button){margin:0}
.emby-details{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.emby-details summary{cursor:pointer;margin-bottom:7px;width:fit-content}
.emby-details p{margin:7px 0}
.media-settings .workflow-switch-row{min-height:46px;padding:7px 0}
.media-schedule{max-width:285px;margin-top:10px}
.media-schedule :deep(.el-form-item){margin-bottom:0}
</style>
