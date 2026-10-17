<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { del, get, post } from '../api/client';
import { object, records, text } from '../api/domain';

const accounts = ref<Record<string, unknown>[]>([]);
const accountId = ref('');
const device = ref('alipaymini');
const cookie = ref('');
const replaceAccount = ref(false);
const officialDelete = ref(false);
const generatedGovernance = ref(false);
const authStatus = ref('');
const qrUrl = ref('');
const flowId = ref('');
const message = ref('');
const error = ref('');
const busy = ref(false);
let pollTimer: number | undefined;

const current = () => accounts.value.find((a) => text(a.id) === accountId.value);

async function loadAccounts() {
  try {
    const data = object(await get<Record<string, unknown>>('/api/cloud/status'));
    accounts.value = records(data.accounts);
    if (!accountId.value && accounts.value.length) accountId.value = text(accounts.value[0]!.id);
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function importCookie() {
  if (!cookie.value.trim()) {
    message.value = '请先粘贴 Cookie';
    return;
  }
  busy.value = true;
  message.value = '正在检测并加密保存…';
  try {
    const data = object(
      await post<Record<string, unknown>>('/api/cloud/accounts/cookie', {
        cookie: cookie.value,
        account_id: replaceAccount.value ? accountId.value || null : null,
      }),
    );
    accountId.value = text(data.account_id || data.id);
    message.value = 'Cookie 已检测并加密保存';
    cookie.value = '';
    await loadAccounts();
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function startQr() {
  busy.value = true;
  try {
    const data = object(await post<Record<string, unknown>>('/api/cloud/qr', { device: device.value }));
    flowId.value = text(data.flow_id || data.id);
    message.value = '请用 115 扫码登录';
    await pollQr(true);
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function pollQr(first = false) {
  if (!flowId.value) return;
  try {
    const img = await fetch(`/api/cloud/qr/${encodeURIComponent(flowId.value)}/image`);
    if (img.ok) {
      const blob = await img.blob();
      qrUrl.value = URL.createObjectURL(blob);
    }
    const data = object(
      await post<Record<string, unknown>>(`/api/cloud/qr/${encodeURIComponent(flowId.value)}/poll`, {}),
    );
    if (data.authorized) {
      message.value = '扫码登录成功';
      stopPoll();
      await loadAccounts();
      return;
    }
    if (data.expired) {
      message.value = '二维码已过期，请重新扫码';
      stopPoll();
      return;
    }
    message.value = first ? '等待扫码…' : text(data.status || '等待扫码…');
  } catch (e) {
    message.value = (e as Error).message;
    stopPoll();
  }
}
function stopPoll() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = undefined;
  qrUrl.value = '';
  flowId.value = '';
}
async function check() {
  if (!accountId.value) return;
  busy.value = true;
  try {
    const data = object(await post<Record<string, unknown>>(`/api/cloud/accounts/${encodeURIComponent(accountId.value)}/check`, {}));
    authStatus.value = text(data.status || '检查完成');
  } catch (e) {
    authStatus.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function disconnect() {
  if (!accountId.value) return;
  await del(`/api/cloud/accounts/${encodeURIComponent(accountId.value)}`);
  accountId.value = '';
  await loadAccounts();
}
onMounted(() => {
  void loadAccounts();
  void check();
});
</script>
<template>
  <section class="panel">
    <h3 data-icon="server">115 登录凭证</h3>
    <p v-if="current()" class="hint">
      当前账号：{{ text(current()!.name) }}（{{ text(current()!.provider) }} · {{ text(current()!.uid) }}）
    </p>
    <p class="hint">使用 Cookie 完成文件读写。选择与常用客户端不同的设备，可减少同类客户端登录冲突。</p>
    <p v-if="message" class="hint" role="status">{{ message }}</p>
    <p v-if="error" class="hint">{{ error }}</p>

    <div class="cw-form-grid">
      <label
        >登录客户端
        <select v-model="device">
          <option value="alipaymini">支付宝小程序</option>
          <option value="ios">iOS · 115生活</option>
          <option value="ipad">iPad · 115生活</option>
          <option value="115ios">iOS · 115网盘</option>
          <option value="115ipad">iPad · 115网盘</option>
          <option value="android">Android</option>
          <option value="tv">电视端</option>
          <option value="web">网页端</option>
        </select></label
      >
    </div>
    <div style="display: flex; gap: 8px; align-items: center; margin: 8px 0">
      <button class="btn" :disabled="busy" @click="startQr">扫码登录 115</button>
      <img v-if="qrUrl" :src="qrUrl" width="220" height="220" alt="115 登录二维码" />
    </div>

    <details class="cw-settings-group">
      <summary>手动填写 Cookie</summary>
      <label
        >登录 Cookie
        <textarea v-model="cookie" rows="3" autocomplete="off" spellcheck="false" placeholder="粘贴完整的 115 Cookie"></textarea
      ></label>
      <label
        ><input v-model="replaceAccount" type="checkbox" />更新当前账号凭证（校验为同一 UID）</label
      >
      <button class="btn" :disabled="busy" @click="importCookie">检测并保存 Cookie</button>
    </details>

    <div class="cw-settings-group" style="display: flex; gap: 8px; align-items: center">
      <button class="btn gray" :disabled="busy" @click="check">检测 Cookie 登录</button>
      <button class="btn gray" :disabled="!accountId" @click="disconnect">断开当前账号</button>
      <span class="hint">{{ authStatus }}</span>
    </div>
  </section>
</template>
