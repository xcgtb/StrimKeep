<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { del, get, post } from '../api/client';
import { object, records, text } from '../api/domain';

const accounts = ref<Record<string, unknown>[]>([]);
const accountId = ref('');
const channels = ref<Record<string, unknown>[]>([]);
const inbox = ref<Record<string, unknown>[]>([]);
const name = ref('');
const urls = ref('');
const message = ref('');
const error = ref('');
const busy = ref(false);

async function loadAccounts() {
  const data = object(await get<Record<string, unknown>>('/api/cloud/status'));
  accounts.value = records(data.accounts);
  if (!accountId.value && accounts.value.length) accountId.value = text(accounts.value[0]!.id);
}
async function loadChannels() {
  if (!accountId.value) return;
  try {
    const data = object(
      await get<Record<string, unknown>>(
        `/api/cloud/accounts/${encodeURIComponent(accountId.value)}/channels`,
      ),
    );
    channels.value = records(data.channels || data.items);
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function loadInbox() {
  if (!accountId.value) return;
  try {
    const data = object(
      await get<Record<string, unknown>>(
        `/api/cloud/accounts/${encodeURIComponent(accountId.value)}/inbox`,
      ),
    );
    inbox.value = records(data.items || data.records).slice(0, 30);
  } catch {
    inbox.value = [];
  }
}
async function addChannel() {
  if (!accountId.value || !name.value.trim()) return;
  busy.value = true;
  try {
    await post(`/api/cloud/accounts/${encodeURIComponent(accountId.value)}/channels`, {
      name: name.value,
      urls: urls.value
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean),
    });
    name.value = '';
    urls.value = '';
    message.value = '频道已添加';
    await loadChannels();
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function removeChannel(id: string) {
  await del(
    `/api/cloud/accounts/${encodeURIComponent(accountId.value)}/channels/${encodeURIComponent(id)}`,
  );
  await loadChannels();
}
async function testChannel(id: string) {
  busy.value = true;
  try {
    const data = object(
      await post(
        `/api/cloud/accounts/${encodeURIComponent(accountId.value)}/channels/${encodeURIComponent(id)}/test`,
        {},
      ),
    );
    message.value = text(data.status || '测试已提交');
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
watch(accountId, () => {
  void loadChannels();
  void loadInbox();
});
onMounted(async () => {
  await loadAccounts();
  await loadChannels();
  await loadInbox();
});
</script>
<template>
  <section class="panel">
    <div class="row-between">
      <h3 data-icon="list">115 频道监控</h3>
      <select v-model="accountId" aria-label="115 账号">
        <option v-for="account in accounts" :key="text(account.id)" :value="text(account.id)">
          {{ text(account.name || account.id) }}
        </option>
      </select>
    </div>
    <p v-if="message" class="hint" role="status">{{ message }}</p>
    <p v-if="error" class="hint">{{ error }}</p>

    <div class="cw-form-grid">
      <label
        >频道名
        <input v-model="name" placeholder="例如：蓝光站" /></label
      >
      <label
        >订阅链接（每行一个）
        <textarea v-model="urls" rows="3" placeholder="粘贴频道 / 用户订阅链接"></textarea
      ></label>
    </div>
    <button class="btn" :disabled="busy" @click="addChannel">添加频道</button>

    <div v-if="!channels.length" class="list-empty">还没有频道</div>
    <div v-for="channel in channels" :key="text(channel.id)" class="cw-file-row">
      <span class="cw-file-name">{{ text(channel.name || channel.id) }}</span>
      <span class="cw-file-meta">{{ text(channel.source || '') }}</span>
      <button class="btn gray sm" @click="testChannel(text(channel.id))">测试</button>
      <button class="btn gray sm" @click="removeChannel(text(channel.id))">删除</button>
    </div>

    <details class="cw-settings-group">
      <summary>监控与接收历史</summary>
      <div v-if="!inbox.length" class="list-empty">暂无接收记录</div>
      <div v-for="row in inbox" :key="text(row.id)" class="cw-file-row">
        <span class="cw-file-name">{{ text(row.name || row.title) }}</span>
        <span class="cw-file-meta">{{ text(row.source || '') }} · {{ text(row.state || '') }}</span>
      </div>
    </details>
  </section>
</template>
