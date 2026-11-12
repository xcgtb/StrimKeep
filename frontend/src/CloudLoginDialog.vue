<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'

type Client = { value: string; label: string; group: string }
type Challenge = { id: string; image: string; app: string; client_label: string; expires_in: number }
const props = defineProps<{
  modelValue: boolean
  api: (path: string, method?: string, body?: unknown) => Promise<any>
}>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  connected: [clientLabel: string]
}>()
const clients = ref<Client[]>([]), selected = ref(''), qr = ref<Challenge | null>(null)
const loading = ref(false), generating = ref(false), polling = ref(false)
const error = ref(''), state = ref('idle'), remaining = ref(0)
const groups = computed(() => [...new Set(clients.value.map(item => item.group))])
const stateText = computed(() => ({
  waiting: '等待扫码，请使用手机 115 App 扫描并确认',
  scanned: '已扫码，请在手机上确认登录',
  expired: '二维码已过期，请重新生成',
  cancelled: '本次扫码已取消，请重新生成',
  connected: '登录成功，Cookie 已保存',
}[state.value] || ''))
let viewEpoch = 0, challengeEpoch = 0, deadline = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
let clock: ReturnType<typeof setInterval> | undefined

function stopTimers() {
  clearTimeout(pollTimer)
  clearInterval(clock)
  pollTimer = undefined
  clock = undefined
}
function discard(key: string) {
  // Cancelling a dialog never starts a new login; invalidate the old server challenge.
  props.api(`/connections/115/qr/${encodeURIComponent(key)}`, 'DELETE').catch(() => {})
}
function clearChallenge() {
  challengeEpoch++
  stopTimers()
  if (qr.value) discard(qr.value.id)
  qr.value = null
  generating.value = false
  polling.value = false
  state.value = 'idle'
  remaining.value = 0
  error.value = ''
}
async function loadClients(epoch: number) {
  loading.value = true
  error.value = ''
  try {
    const result = await props.api('/connections/115/clients')
    if (epoch !== viewEpoch || !props.modelValue) return
    clients.value = result.items
    selected.value = clients.value.some(item => item.value === result.selected) ? result.selected : ''
  } catch (e) {
    if (epoch === viewEpoch) error.value = (e as Error).message
  } finally {
    if (epoch === viewEpoch) loading.value = false
  }
}
function expire() {
  if (remaining.value > 0 || !qr.value) return
  stopTimers()
  challengeEpoch++
  polling.value = false
  state.value = 'expired'
  discard(qr.value.id)
}
async function generate() {
  if (!selected.value || loading.value || generating.value) return
  clearChallenge()
  const epoch = challengeEpoch
  generating.value = true
  try {
    const result = await props.api('/connections/115/qr', 'POST', { app: selected.value }) as Challenge
    if (epoch !== challengeEpoch || !props.modelValue) { discard(result.id); return }
    qr.value = result
    state.value = 'waiting'
    deadline = Date.now() + result.expires_in * 1000
    remaining.value = result.expires_in
    clock = setInterval(() => {
      remaining.value = Math.max(0, Math.ceil((deadline - Date.now()) / 1000))
      expire()
    }, 1000)
    pollTimer = setTimeout(() => poll(epoch), 1800)
  } catch (e) {
    if (epoch === challengeEpoch) error.value = (e as Error).message
  } finally {
    if (epoch === challengeEpoch) generating.value = false
  }
}
async function poll(epoch = challengeEpoch) {
  if (epoch !== challengeEpoch || !props.modelValue || !qr.value || polling.value || remaining.value <= 0) return
  clearTimeout(pollTimer)
  polling.value = true
  error.value = ''
  try {
    const result = await props.api(`/connections/115/qr/${qr.value.id}/poll`, 'POST')
    if (epoch !== challengeEpoch || !props.modelValue) return
    state.value = result.status
    if (result.status === 'connected') {
      const label = qr.value.client_label
      stopTimers()
      qr.value = null
      emit('connected', label)
      emit('update:modelValue', false)
    } else if (['expired', 'cancelled'].includes(result.status)) {
      stopTimers()
    } else {
      pollTimer = setTimeout(() => poll(epoch), 1800)
    }
  } catch (e) {
    if (epoch === challengeEpoch) error.value = (e as Error).message
    // On a network failure stop automatic retries; the visible check button permits a retry.
  } finally {
    if (epoch === challengeEpoch) polling.value = false
  }
}
watch(selected, () => clearChallenge())
watch(() => props.modelValue, opened => {
  const epoch = ++viewEpoch
  clearChallenge()
  if (opened) loadClients(epoch)
})
onUnmounted(() => { viewEpoch++; clearChallenge() })
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="115 扫码登录"
    width="480px"
    class="cloud-login-dialog"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-form label-position="top" @submit.prevent="generate">
      <el-form-item label="登录客户端">
        <el-select v-model="selected" aria-label="登录客户端" placeholder="请选择登录客户端" :loading="loading" :disabled="loading" class="full" filterable>
          <el-option-group v-for="group in groups" :key="group" :label="group">
            <el-option v-for="client in clients.filter(item => item.group === group)" :key="client.value" :label="client.label" :value="client.value" />
          </el-option-group>
        </el-select>
      </el-form-item>
      <p class="login-client-help">选择本工具使用的登录客户端，扫码仍使用手机 115 App。</p>
      <el-button type="primary" class="full" native-type="submit" :disabled="!selected || loading" :loading="generating">
        {{ qr ? '重新生成二维码' : '生成二维码' }}
      </el-button>
    </el-form>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="login-qr-error" />
    <div v-if="qr" class="login-qr-result">
      <div class="login-client-badge">登录客户端：<strong>{{ qr.client_label }}</strong></div>
      <div class="login-qr-image" :class="{inactive: ['expired', 'cancelled'].includes(state)}">
        <img :src="qr.image" :alt="qr.client_label + ' 登录二维码'" />
        <div v-if="['expired', 'cancelled'].includes(state)" class="login-qr-cover">{{ state === 'expired' ? '二维码已过期' : '扫码已取消' }}</div>
      </div>
      <p class="login-qr-status" role="status">{{ stateText }}</p>
      <p v-if="remaining > 0 && ['waiting', 'scanned'].includes(state)" class="login-qr-countdown">有效期剩余 {{ remaining }} 秒 · 自动检查扫码状态</p>
      <el-button v-if="remaining > 0 && ['waiting', 'scanned'].includes(state)" :loading="polling" @click="poll()">检查扫码状态</el-button>
    </div>
    <div v-else-if="!generating" class="login-qr-placeholder">先选择客户端，再点击生成二维码。</div>
    <template #footer><el-button @click="emit('update:modelValue', false)">关闭</el-button></template>
  </el-dialog>
</template>

<style scoped>
.login-client-help{font-size:12px;line-height:1.8;color:#7b89a1;margin:0 0 16px}
.login-qr-placeholder{margin-top:20px;padding:28px 16px;text-align:center;border:1px dashed #dfe6f2;border-radius:12px;color:#8995a8;font-size:13px}
.login-qr-error{margin-top:18px}
.login-qr-result{text-align:center;margin-top:22px;padding:18px;background:#f7f9fd;border:1px solid #e8edf6;border-radius:12px}
.login-client-badge{font-size:12px;color:#7e8ca3;line-height:1.8;margin-bottom:14px}
.login-client-badge strong{color:#3a5277;font-weight:600}
.login-qr-image{position:relative;width:220px;max-width:100%;margin:auto;background:#fff;border-radius:8px;overflow:hidden}
.login-qr-image img{display:block;width:100%;height:auto}
.login-qr-image.inactive img{opacity:.15;filter:blur(2px)}
.login-qr-cover{position:absolute;inset:0;display:grid;place-items:center;color:#63728c;font-size:14px}
.login-qr-status{font-size:13px;line-height:1.8;margin:16px 0 7px;color:#4c617d}
.login-qr-countdown{font-size:11px;color:#8a97ac;line-height:1.7;margin:0 0 12px}
@media(max-width:480px){.login-qr-result{padding:15px 12px}.login-qr-image{width:190px}.login-qr-status{font-size:12px}}
</style>
