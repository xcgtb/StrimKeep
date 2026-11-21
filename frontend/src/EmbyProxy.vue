<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'
import EmbyMapping from './EmbyMapping.vue'
import EmbyProxyServers from './EmbyProxyServers.vue'
import LibraryCovers from './LibraryCovers.vue'

const props = defineProps<{ api: (path: string, method?: string, body?: unknown) => Promise<any> }>()
const route = useRoute()
const mediaSection = ref<HTMLElement>()
const busy = ref(false)
const error = ref('')
const connections = ref({ emby_url: '', emby_key: '' })
const mask = ref<any>({})
const checks = ref<Record<string, string>>({})
const connectionRevision = ref(0)
const media = ref<any>({ enabled: true, extract: true, cron_enabled: false, cron: '0 4 * * *', timezone: 'Asia/Shanghai' })
const mediaBusy = ref(false)
const mediaMsg = ref('')
const mediaStatus = ref<any>(null)
const mediaFailures = ref<any>(null)
const mediaError = ref('')

async function load() {
  error.value = ''
  try {
    const info: any = await props.api('/settings')
    connections.value.emby_url = info.emby_url || ''
    mask.value = info
  } catch (e) { error.value = (e as Error).message }
  try { media.value = { ...media.value, ...await props.api('/emby/media-info/config') } }
  catch (e) { error.value = (e as Error).message }
}
async function saveConnections() {
  busy.value = true; error.value = ''
  try {
    const body: any = { emby_url: connections.value.emby_url }
    if (connections.value.emby_key) body.emby_key = connections.value.emby_key
    mask.value = await props.api('/settings', 'PUT', body)
    connections.value.emby_key = ''
    connectionRevision.value++
    ElMessage.success('Emby 服务连接已保存')
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function checkEmby() {
  busy.value = true; checks.value.emby = ''
  try {
    await props.api('/connections/emby/test', 'POST',
      { emby_url: connections.value.emby_url, emby_key: connections.value.emby_key })
    checks.value.emby = '填写的地址和密钥（留空时使用已保存的值）连接正常'
    ElMessage.success('连接正常')
  } catch (e) { checks.value.emby = (e as Error).message }
  finally { busy.value = false }
}
async function saveMedia() {
  busy.value = true
  try {
    media.value = { ...media.value, ...await props.api('/emby/media-info/config', 'PUT', {
      enabled: !!media.value.enabled, extract: !!media.value.extract,
      cron_enabled: !!media.value.cron_enabled, cron: media.value.cron }) }
    ElMessage.success('媒体信息设置已保存')
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function injectMedia() {
  mediaBusy.value = true; mediaMsg.value = ''
  try {
    const r: any = await props.api('/emby/media-info/inject', 'POST')
    mediaMsg.value = r.injected ? `已写入 ${r.injected} 条（${r.streams} 条流）` : `没有需要补的条目（候选 ${r.candidates} 条）`
    ElMessage.success(mediaMsg.value)
  } catch (e) { mediaMsg.value = (e as Error).message }
  finally { mediaBusy.value = false }
}
async function loadMediaStatus() {
  mediaError.value = ''
  try {
    const [status, failures] = await Promise.all([
      props.api('/emby/media-info/status'), props.api('/emby/media-info/failures?limit=200')])
    mediaStatus.value = status; mediaFailures.value = failures
  } catch (e) { mediaError.value = (e as Error).message }
}
onMounted(() => {
  void load()
  if (route.hash === '#media-info') mediaSection.value?.scrollIntoView()
})
</script>


<template>
<div class="page-title"><div><h1>Emby 反代</h1><p class="muted">Emby 服务连接、媒体信息写入、反代播放与路径映射。</p></div></div>
<el-alert v-if="error" :title="error" type="error" :closable="false" />
<section class="panel settings-panel"><h2>Emby 服务连接</h2>
  <el-form label-position="top" :disabled="busy">
    <el-form-item label="Emby 服务地址"><el-input v-model="connections.emby_url" placeholder="http://192.0.2.10:8096" /></el-form-item>
    <el-form-item label="Emby API Key"><el-input v-model="connections.emby_key" type="password" autocomplete="new-password" show-password :placeholder="mask.emby_key_configured ? '已保存，留空保留原值' : '填写 API Key'" /></el-form-item>
  </el-form>
  <el-button :loading="busy" @click="checkEmby">检查连接</el-button>
  <span class="hint">{{ checks.emby }}</span>
  <el-button type="primary" :loading="busy" @click="saveConnections">保存服务连接</el-button>
  <p class="hint">STRM 播放基础地址不在这里维护，请到「STRM 管理 → 播放地址」。</p>
</section>
<LibraryCovers :api="api" :connection-revision="connectionRevision" />
<section id="media-info" ref="mediaSection" class="panel settings-panel"><h2>媒体信息写入</h2>
  <p class="hint">总开关关闭后暂停提取、补扫并停止向 Emby 写入媒体信息；已有文件与待处理队列保留。</p>
  <p class="hint">STRM 同级的 <code>*-media-info.json</code> 是 StrimKeep 生成的媒体信息，Emby 不会直接读取；Emby 只读 NFO 和图片。编码、分辨率、音轨、章节需要写入 Emby 的 library.db。</p>
  <details class="media-diagnostics" open><summary>先建库还是先整理？立即补写是做什么的？</summary>
    <p><strong>先建库后整理：</strong>Emby 里已经有这个媒体库时，新条目整理后会自动提取并立即写入 Emby，一般不需要手动补写。</p>
    <p><strong>先整理后建库：</strong>整理时 Emby 还没有这个库或条目，STRM、NFO、图片和媒体信息 JSON 已经生成，但媒体流没写进 Emby；把目录加入 Emby 媒体库并刷新后，点一次「立即补写」补齐。</p>
    <p><strong>立即补写：</strong>扫描 Emby 里缺少视频流的 STRM 条目，按同级媒体信息 JSON 写入媒体流和章节；已经有视频流的条目会跳过，即使信息不完整也不会自动覆盖。</p>
    <p><strong>定时补写：</strong>按 cron 做同样的兜底，适合旧条目、漏写或 Emby 数据库恢复后补齐。</p>
    <p class="hint">立即补写和定时补写都需要 StrimKeep 容器挂载 Emby 的 data 目录，并配置 STRIMKEEP_EMBY_LIBRARY_DB。</p>
  </details>
  <el-form label-position="top" :disabled="busy">
    <el-form-item label="启用本项目媒体信息处理"><el-switch v-model="media.enabled" /></el-form-item>
    <el-form-item label="新入库自动提取"><el-switch v-model="media.extract" :disabled="!media.enabled" /></el-form-item>
    <el-form-item label="定时补写缺失信息">
      <el-switch v-model="media.cron_enabled" :disabled="!media.enabled" />
      <el-input v-model="media.cron" :disabled="!media.enabled || !media.cron_enabled" placeholder="0 4 * * *" style="max-width:200px;margin-left:10px" />
    </el-form-item>
  </el-form>
  <p class="hint">定时时区：{{ media.timezone }}</p>
  <el-button type="primary" :loading="busy" @click="saveMedia">保存媒体信息设置</el-button>
  <el-button :loading="mediaBusy" :disabled="!media.enabled" @click="injectMedia">立即补写</el-button>
  <span class="hint">{{ mediaMsg }}</span>
  <details class="media-diagnostics"><summary>媒体信息写入状态与失败记录</summary>
    <el-button @click="loadMediaStatus">读取状态</el-button>
    <el-alert v-if="mediaError" :title="mediaError" type="error" :closable="false" />
    <template v-if="mediaStatus">
      <p class="hint">数据库：{{ !mediaStatus.configured ? '未配置' : mediaStatus.exists ? '已找到' : '未找到' }} · 待写入：{{ mediaStatus.pending ?? '暂不可用' }} · 未修复：{{ mediaFailures?.count ?? 0 }}</p>
      <p v-if="mediaStatus.hint" class="hint">{{ mediaStatus.hint }}</p>
      <details v-for="row in mediaFailures?.items || []" :key="row.item_id" class="media-failure">
        <summary>{{ row.name || row.item_id }} · {{ row.reason_text }}</summary>
        <p class="file-path">{{ row.path }}</p>
      </details>
    </template>
  </details>
</section>
<section class="panel settings-panel"><h2>Emby 反代播放</h2><EmbyProxyServers :api="api" /></section>
<section class="panel settings-panel"><h2>Emby 路径映射</h2><EmbyMapping :api="api" /></section>
</template>

<style scoped>
#media-info { scroll-margin-top: 80px }
</style>
