<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

type Server = {
  id: string
  name: string
  port: number
  enabled: boolean
  running: boolean
  upstream: string
  upstream_override: string
  api_key_override_configured: boolean
  emby_strm_path: string
}
type Payload = { servers: Server[]; emby_url: string; emby_strm_path: string; id?: string }
type Draft = { name: string; port?: number; source: 'main' | 'custom'; upstream: string; key: string; path: string }
type Probe = { ok: boolean; message: string; latency_ms?: number }
type Play = { id: number; at: number; user_agent: string; target: string; filled: number; outcome: string; reason: string }

const props = defineProps<{ api: (path: string, method?: string, body?: unknown) => Promise<any>; revision: number }>()
const servers = ref<Server[]>([])
const defaultUrl = ref('')
const defaultPath = ref('')
const busy = ref('')
const error = ref('')
const formError = ref('')
const editOpen = ref(false)
const editing = ref<Server>()
const draft = ref<Draft>({ name: '', source: 'main', upstream: '', key: '', path: '' })
const result = ref<Probe>()
const playsOpen = ref(false)
const playsFor = ref('')
const plays = ref<Play[]>([])

const serverPath = (s: Server) => '/emby-proxy/servers/' + s.id
const custom = (s: Server) => !!(s.upstream_override || s.api_key_override_configured)
const urlKey = (value: string) => value.trim().replace(/\/+$/, '')

function stateOf(s: Server) {
  if (s.running) return '运行中'
  if (!s.upstream) return '待配置'
  return s.enabled ? '未监听' : '已停用'
}

function reasonText(raw: string) {
  const labels: Record<string, string> = {
    no_sources: 'Emby 未返回可播放媒体源', no_identity: '媒体源未匹配到 StrimKeep 播放地址',
    share_missing: '分享记录不存在', share_revoked: '分享记录已撤销', timeout: '115 取链超时',
    'cloud:RATE_LIMITED': '115 限流', 'cloud:LOGIN_REQUIRED': '115 未登录',
    'cloud:LOGIN_INVALID': '115 登录失效', 'cloud:SHARE_NOT_FOUND': '分享或文件已失效',
    'cloud:FILE_NOT_FOUND': '网盘文件不存在',
  }
  return (raw || '').split(',').filter(Boolean).map(code => labels[code] || code).join('；')
}

function update(payload: Payload) {
  servers.value = payload.servers
  defaultUrl.value = payload.emby_url || ''
  defaultPath.value = payload.emby_strm_path
}

async function run(action: string, task: () => Promise<void>, inForm = false) {
  if (busy.value) return
  busy.value = action
  const message = inForm ? formError : error
  message.value = ''
  try { await task() }
  catch (e) { message.value = (e as Error).message }
  finally { busy.value = '' }
}

async function load() {
  await run('load', async () => update(await props.api('/emby-proxy')))
}

function edit(server: Server, newServer = false) {
  editing.value = server
  draft.value = {
    name: server.name, port: server.port, source: newServer || custom(server) ? 'custom' : 'main',
    upstream: custom(server) ? server.upstream : '', key: '', path: server.emby_strm_path,
  }
  formError.value = ''
  result.value = undefined
  editOpen.value = true
}

async function add() {
  await run('add', async () => {
    const response: Payload = await props.api('/emby-proxy/servers', 'POST')
    update(response)
    const created = response.servers.find(s => s.id === response.id)
    if (created) edit(created, true)
  })
}

function connectionDraft() {
  if (draft.value.source === 'main') return { upstream: '', api_key: '' }
  const upstream = urlKey(draft.value.upstream)
  if (!upstream) throw new Error('请填写独立 Emby 的服务地址。')
  const apiKey = draft.value.key.trim()
  return apiKey ? { upstream, api_key: apiKey } : { upstream }
}

async function save() {
  const server = editing.value
  if (!server) return
  await run('save', async () => {
    const { name, port, path } = draft.value
    if (port === undefined || !Number.isInteger(port) || port < 1 || port > 65535) throw new Error('请填写有效的监听端口。')
    const response: Payload = await props.api(serverPath(server), 'PUT', {
      ...connectionDraft(), name, port, emby_strm_path: path,
    })
    update(response)
    editOpen.value = false
    const saved = response.servers.find(s => s.id === server.id)
    if (saved?.enabled && !saved.running) ElMessage.warning('设置已保存，端口尚未监听。')
    else ElMessage.success('反代设置已保存')
  }, true)
}

async function probe() {
  const server = editing.value
  if (!server) return
  result.value = undefined
  await run('test', async () => {
    result.value = await props.api(serverPath(server) + '/test', 'POST', connectionDraft())
  }, true)
}

async function toggle(server: Server) {
  await run(server.id, async () => {
    const response: Payload = await props.api(serverPath(server) + (server.enabled ? '/disable' : '/enable'), 'POST')
    update(response)
    const saved = response.servers.find(s => s.id === server.id)
    if (saved?.enabled && !saved.running) ElMessage.warning('端口未监听，请检查端口占用。')
  })
}

async function remove() {
  const server = editing.value
  if (!server) return
  try {
    await ElMessageBox.confirm('移除后，播放器将无法连接这条反代。', '移除「' + server.name + '」', {
      type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消',
    })
  } catch { return }
  await run('remove', async () => {
    update(await props.api(serverPath(server), 'DELETE'))
    editOpen.value = false
    ElMessage.success('已移除反代')
  }, true)
}

async function showPlays(server: Server) {
  await run('plays', async () => {
    const data = await props.api(serverPath(server) + '/plays')
    plays.value = data.plays || []
    playsFor.value = server.name
    playsOpen.value = true
  })
}

watch(draft, () => { result.value = undefined }, { deep: true })
watch(() => props.revision, load)
onMounted(load)
</script>

<template>
  <section class="panel proxy-panel">
    <header class="proxy-head">
      <div>
        <h2>反代实例</h2>
        <span v-if="servers.length" class="hint">{{ servers.filter(s => s.running).length }} / {{ servers.length }} 运行中</span>
      </div>
      <el-button type="primary" :loading="busy === 'add'" :disabled="!!busy && busy !== 'add'" @click="add">新增反代</el-button>
    </header>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-empty v-if="!servers.length && !busy && !error" :image-size="55" description="暂无反代实例" />
    <div v-for="server in servers" :key="server.id" class="proxy-row">
      <div class="proxy-info">
        <div class="proxy-name">
          <strong>{{ server.name }}</strong>
          <el-tag size="small" effect="plain" :type="server.running ? 'success' : server.enabled ? 'warning' : 'info'">{{ stateOf(server) }}</el-tag>
        </div>
        <div class="proxy-endpoints">
          <span>端口 <b>{{ server.port }}</b></span>
          <span>{{ custom(server) ? '独立连接' : '主 Emby' }}</span>
        </div>
        <div class="proxy-upstream" :title="server.upstream">{{ server.upstream || '请配置 Emby 地址' }}</div>
      </div>
      <div class="proxy-actions">
        <el-button :disabled="!!busy" @click="showPlays(server)">取链记录</el-button>
        <el-button :loading="busy === server.id" :disabled="!!busy && busy !== server.id" @click="toggle(server)">{{ server.enabled ? '停用' : '启用' }}</el-button>
        <el-button type="primary" plain :disabled="!!busy" @click="edit(server)">配置</el-button>
      </div>
    </div>
    <details class="proxy-help">
      <summary>播放器连接说明</summary>
      <p>在播放器中填写 StrimKeep 主机地址和该实例的监听端口。Docker 使用 Bridge 网络时，需映射对应端口。</p>
    </details>
  </section>

  <el-dialog v-model="editOpen" :title="editing ? '反代配置 · ' + editing.name : '反代配置'" width="min(600px, 94vw)" :close-on-click-modal="false" @closed="draft.key = ''">
    <el-form label-position="top" :disabled="!!busy" @submit.prevent="save">
      <div class="proxy-form-grid">
        <el-form-item label="实例名称"><el-input v-model="draft.name" maxlength="40" /></el-form-item>
        <el-form-item label="监听端口"><el-input-number v-model="draft.port" :min="1" :max="65535" :precision="0" controls-position="right" /></el-form-item>
      </div>
      <el-form-item label="Emby 连接">
        <el-radio-group v-model="draft.source">
          <el-radio-button value="main">使用主 Emby</el-radio-button>
          <el-radio-button value="custom">独立连接</el-radio-button>
        </el-radio-group>
        <span v-if="draft.source === 'main'" class="proxy-default">{{ defaultUrl || '请先填写主 Emby 连接' }}</span>
      </el-form-item>
      <template v-if="draft.source === 'custom'">
        <el-form-item label="Emby 服务地址"><el-input v-model="draft.upstream" placeholder="http://192.0.2.10:8096" /></el-form-item>
        <el-form-item label="Emby API Key">
          <el-input v-model="draft.key" type="password" show-password autocomplete="new-password"
            :placeholder="editing?.api_key_override_configured && urlKey(draft.upstream) === urlKey(editing.upstream) ? '已保存，留空保留原值' : '填写这台 Emby 的 API Key'" />
        </el-form-item>
      </template>
      <details class="proxy-help" :open="!!editing?.emby_strm_path">
        <summary>独立 STRM 路径映射</summary>
        <el-form-item label="这台 Emby 的 STRM 挂载目录">
          <el-input v-model="draft.path" :placeholder="'留空沿用 ' + (defaultPath || '/media')" />
        </el-form-item>
        <p>指向与 StrimKeep 共用的 STRM 文件。</p>
      </details>
    </el-form>
    <el-alert v-if="formError" :title="formError" type="error" :closable="false" />
    <el-alert v-if="result" :title="result.message + (result.latency_ms !== undefined ? ' · ' + result.latency_ms + ' ms' : '')" :type="result.ok ? 'success' : 'error'" :closable="false" />
    <template #footer>
      <div class="proxy-dialog-actions">
        <el-button text type="danger" :loading="busy === 'remove'" :disabled="!!busy" @click="remove">移除</el-button>
        <div class="proxy-actions">
          <el-button :loading="busy === 'test'" :disabled="!!busy && busy !== 'test'" @click="probe">测试连接</el-button>
          <el-button type="primary" :loading="busy === 'save'" :disabled="!!busy && busy !== 'save'" @click="save">保存</el-button>
        </div>
      </div>
    </template>
  </el-dialog>

  <el-dialog v-model="playsOpen" :title="'取链记录 · ' + playsFor" width="min(700px, 94vw)">
    <el-empty v-if="!plays.length" :image-size="55" description="暂无取链记录" />
    <div v-else class="proxy-plays">
      <article v-for="play in plays" :key="play.id" class="proxy-play">
        <div class="proxy-play-head">
          <strong>{{ play.user_agent || '未知客户端' }}</strong>
          <span>{{ new Date(play.at * 1000).toLocaleString() }}</span>
          <el-tag size="small" effect="plain" :type="play.outcome === 'ok' ? 'success' : play.outcome === 'empty' ? 'info' : 'danger'">{{ play.outcome === 'ok' ? '已注入 ' + play.filled : play.outcome === 'empty' ? '无需注入' : '失败' }}</el-tag>
        </div>
        <p>{{ play.target }}</p>
        <p v-if="play.reason">{{ reasonText(play.reason) }}</p>
      </article>
    </div>
  </el-dialog>
</template>

<style scoped>
.proxy-head,.proxy-row,.proxy-name,.proxy-actions,.proxy-dialog-actions,.proxy-play-head{display:flex;align-items:center;gap:10px}
.proxy-head,.proxy-row,.proxy-dialog-actions{justify-content:space-between}
.proxy-head{margin-bottom:14px}
.proxy-head h2{margin:0 0 3px}
.proxy-row{padding:14px 0;border-top:1px solid var(--line)}
.proxy-info{min-width:0}
.proxy-name{flex-wrap:wrap;overflow-wrap:anywhere}
.proxy-endpoints{display:flex;gap:14px;margin:5px 0 2px;color:var(--muted);font-size:12px;flex-wrap:wrap}
.proxy-endpoints b{font-variant-numeric:tabular-nums;color:var(--text)}
.proxy-upstream,.proxy-default{font-size:13px;color:var(--muted);overflow-wrap:anywhere}
.proxy-default{flex-basis:100%;margin-top:5px}
.proxy-actions{gap:6px;flex-wrap:wrap;justify-content:flex-end}
.proxy-actions :deep(.el-button){margin:0}
.proxy-help{margin-top:7px;color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:10px}
.proxy-help summary{cursor:pointer;width:fit-content}
.proxy-help p{margin:6px 0 0}
.proxy-help :deep(.el-form-item){margin:10px 0 0}
.proxy-form-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(138px,.58fr);gap:12px}
.proxy-form-grid :deep(.el-input-number){width:100%}
.proxy-dialog-actions{gap:12px}
.proxy-plays{max-height:60vh;overflow:auto}
.proxy-play{padding:10px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.proxy-play-head{flex-wrap:wrap}
.proxy-play-head span,.proxy-play p{font-size:12px;color:var(--muted)}
.proxy-play p{margin:4px 0 0}
@media(max-width:620px){
  .proxy-row{align-items:flex-start;flex-direction:column;gap:8px}
  .proxy-row .proxy-actions{justify-content:flex-end;width:100%}
}
</style>
