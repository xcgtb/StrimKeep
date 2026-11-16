<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import EmbyMapping from './EmbyMapping.vue'
import EmbyProxyServers from './EmbyProxyServers.vue'

const props = defineProps<{ api: (path: string, method?: string, body?: unknown) => Promise<any> }>()
const busy = ref(false)
const error = ref('')
const connections = ref({ emby_url: '', emby_key: '' })
const mask = ref<any>({})
const checks = ref<Record<string, string>>({})
const coverBusy = ref(false)
const coverMsg = ref('')
const coverStatus = ref<any>(null)
const coverError = ref('')
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
async function runCover() {
  coverBusy.value = true; coverMsg.value = ''
  try { await props.api('/cover/run', 'POST'); coverMsg.value = '封面补齐已开始，可在实时日志查看进度。'; await loadCoverStatus() }
  catch (e) { coverMsg.value = (e as Error).message }
  finally { coverBusy.value = false }
}
async function loadCoverStatus() {
  try { coverStatus.value = await props.api('/cover/status'); coverError.value = '' }
  catch (e) { coverError.value = (e as Error).message }
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
onMounted(load)
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
<section class="panel settings-panel"><h2>Emby 封面</h2>
  <p class="hint">补齐媒体库封面（Primary 图片），任务在后台执行。</p>
  <el-button :loading="coverBusy" @click="runCover">立即补齐封面</el-button>
  <el-button @click="loadCoverStatus">查看封面任务状态</el-button>
  <span class="hint">{{ coverMsg }}</span>
  <span v-if="coverError" class="hint">读取失败：{{ coverError }}</span>
  <p v-if="coverStatus" class="hint">{{ coverStatus.running ? '封面任务进行中' : '当前无封面任务' }}<template v-if="coverStatus.last">；上次成功：扫描 {{ coverStatus.last.scanned }}，写入 {{ coverStatus.last.written }}，已有 {{ coverStatus.last.skipped }}，无图源 {{ coverStatus.last.no_image }}，未映射 {{ coverStatus.last.unmapped }}</template></p>
</section>
<section class="panel settings-panel"><h2>媒体信息写入</h2>
  <p class="hint">总开关关闭后暂停提取、补扫并停止向 Emby 写入媒体信息；已有文件与待处理队列保留。</p>
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
