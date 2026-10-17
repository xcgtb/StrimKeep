<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { get, post, query } from '../api/client';
import { object, records, text } from '../api/domain';

const route = useRoute();
const accounts = ref<Record<string, unknown>[]>([]);
const accountId = ref('');
const tab = computed(() => (String(route.query.tab) === 'offline' ? 'offline' : 'batch'));

const link = ref('');
const password = ref('');
const autoOrganize = ref(false);
const items = ref<Record<string, unknown>[]>([]);
const selected = ref<Set<string>>(new Set());
const busy = ref(false);
const message = ref('');
const error = ref('');
const offline = ref<Record<string, unknown>[]>([]);
const previewId = ref('');

function keyOf(item: Record<string, unknown>) {
  return text(item.id || item.file_id || item.name);
}
async function loadAccounts() {
  try {
    const data = object(await get<Record<string, unknown>>('/api/cloud/status'));
    accounts.value = records(data.accounts);
    if (!accountId.value && accounts.value.length) accountId.value = text(accounts.value[0]!.id);
  } catch {
    /* 忽略 */
  }
}
async function preview() {
  if (!link.value.trim()) return;
  busy.value = true;
  error.value = '';
  message.value = '读取分享内容…';
  try {
    const data = object(
      await post<Record<string, unknown>>('/api/cloud/resources/preview', {
        link: link.value,
        password: password.value || undefined,
      }),
    );
    items.value = records(data.items || data.files || data.entries);
    previewId.value = text(data.preview_id || data.id);
    selected.value = new Set();
    message.value = `读取到 ${items.value.length} 项，勾选后转存`;
  } catch (e) {
    error.value = (e as Error).message;
    message.value = '';
  } finally {
    busy.value = false;
  }
}
async function receive() {
  if (!items.value.length) return;
  busy.value = true;
  message.value = '已提交转存任务…';
  try {
    const chosen = items.value
      .filter((item) => selected.value.has(keyOf(item)))
      .map((item) => ({ id: text(item.id), file_id: text(item.file_id), name: text(item.name) }));
    await post('/api/cloud/resources/receive', {
      link: link.value,
      password: password.value || undefined,
      items: chosen,
      auto_organize: autoOrganize.value,
      preview_id: previewId.value || undefined,
    });
    message.value = `已提交 ${chosen.length} 项转存`;
  } catch (e) {
    error.value = (e as Error).message;
    message.value = '';
  } finally {
    busy.value = false;
  }
}
async function loadOffline() {
  try {
    const data = object(await get<Record<string, unknown>>(query('/api/cloud/resources/offline', { account_id: accountId.value })));
    offline.value = records(data.items || data.records).slice(0, 50);
  } catch {
    offline.value = [];
  }
}
watch(tab, (value) => {
  if (value === 'offline') void loadOffline();
});
onMounted(async () => {
  await loadAccounts();
  if (tab.value === 'offline') void loadOffline();
});
</script>
<template>
  <section class="panel">
    <div class="segmented" style="margin-bottom: 12px">
      <RouterLink to="/cloud/receive" custom v-slot="{ href, navigate, isActive }">
        <a :href="href" :class="{ active: isActive }" @click="navigate">链接转存</a>
      </RouterLink>
      <RouterLink to="/cloud/receive?tab=offline" custom v-slot="{ href, navigate, isActive }">
        <a :href="href" :class="{ active: isActive }" @click="navigate">离线转存</a>
      </RouterLink>
    </div>

    <div style="margin: 8px 0">
      <select v-model="accountId" aria-label="115 账号" @change="tab === 'offline' && loadOffline()">
        <option v-for="account in accounts" :key="text(account.id)" :value="text(account.id)">{{ text(account.name || account.id) }}</option>
      </select>
    </div>

    <div v-if="tab === 'batch'">
      <h3 data-icon="share">链接转存与资源接收</h3>
      <div class="cw-form-grid">
        <label
          >115 分享链接
          <input v-model="link" placeholder="粘贴115分享链接或复制文案" /></label
        >
        <label
          >提取码
          <input v-model="password" placeholder="链接已携带可留空" /></label
        >
      </div>
      <label style="display: flex; gap: 8px; align-items: center"
        ><input v-model="autoOrganize" type="checkbox" />转存后自动整理（是否生成 STRM 取决于整理配置）</label
      >
      <div style="display: flex; gap: 8px; margin: 10px 0">
        <button class="btn" :disabled="busy" @click="preview">读取分享内容</button>
        <button v-if="items.length" class="btn" :disabled="busy || !selected.size" @click="receive">
          转存所选（{{ selected.size }}）
        </button>
      </div>
      <p class="hint" role="status">{{ message }}</p>
      <p v-if="error" class="hint">{{ error }}</p>

      <div v-for="item in items" :key="keyOf(item)" class="cw-file-row">
        <label style="display: flex; gap: 8px; align-items: center; cursor: pointer">
          <input
            type="checkbox"
            :checked="selected.has(keyOf(item))"
            @change="
              () => {
                const next = new Set(selected);
                next.has(keyOf(item)) ? next.delete(keyOf(item)) : next.add(keyOf(item));
                selected = next;
              }
            "
          />
          <span class="cw-file-name">{{ text(item.name) }}</span>
        </label>
        <span class="cw-file-meta">{{ text(item.size ? item.size : '') }}</span>
      </div>
      <div v-if="!items.length" class="list-empty">粘贴分享链接后点「读取分享内容」</div>
    </div>

    <div v-else>
      <h3 data-icon="download">离线转存记录</h3>
      <div v-if="!offline.length" class="list-empty">暂无离线转存记录</div>
      <div v-for="row in offline" :key="text(row.id)" class="cw-file-row">
        <span class="cw-file-name">{{ text(row.name || row.title || row.id) }}</span>
        <span class="cw-file-meta">{{ text(row.state || '') }} · {{ text(row.source || '') }}</span>
      </div>
    </div>
  </section>
</template>
