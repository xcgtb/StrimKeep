<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { del, get, post, query } from '../api/client';
import { object, records, text } from '../api/domain';

const accounts = ref<Record<string, unknown>[]>([]);
const accountId = ref('');
const assets = ref<Record<string, unknown>[]>([]);
const rules = ref<Record<string, unknown>[]>([]);
const offset = ref(0);
const hasMore = ref(false);
const loading = ref(false);
const busy = ref(false);
const error = ref('');
const message = ref('');
const sourceOpen = ref('');
const sourceDetail = ref<Record<string, unknown>>({});

const stateLabels: Record<string, string> = {
  active: '可播放',
  revoked: '已撤销',
  disconnected: '账号已断开',
  removed: '源文件已移出扫描范围',
  publishing: '正在生成',
  needs_review: '需要核实',
};

async function loadAccounts() {
  try {
    const data = object(await get<Record<string, unknown>>('/api/cloud/status'));
    accounts.value = records(data.accounts);
    if (!accountId.value && accounts.value.length) accountId.value = text(accounts.value[0]!.id);
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function loadAssets(more = false) {
  if (!accountId.value) return;
  loading.value = true;
  error.value = '';
  try {
    const data = object(
      await get<Record<string, unknown>>(
        query('/api/cloud/assets', {
          account_id: accountId.value,
          offset: more ? offset.value : 0,
        }),
      ),
    );
    const items = records(data.items);
    assets.value = more ? [...assets.value, ...items] : items;
    offset.value = Number(data.offset ?? 0) + items.length;
    hasMore.value = data.has_more === true;
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
async function loadRules() {
  try {
    const data = object(await get<Record<string, unknown>>('/api/cloud/rules'));
    rules.value = records(data.rules || data.items);
  } catch {
    rules.value = [];
  }
}
async function sync() {
  if (!accountId.value) return;
  busy.value = true;
  message.value = '已提交全量扫描，正在后台生成 / 更新 STRM…';
  try {
    await post('/api/cloud/strm/sync', { account_id: accountId.value });
    message.value = '同步任务已提交';
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function revoke(id: string) {
  try {
    await post(`/api/cloud/assets/${encodeURIComponent(id)}/revoke`, {});
    await loadAssets();
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function toggleSource(id: string) {
  if (sourceOpen.value === id) {
    sourceOpen.value = '';
    return;
  }
  sourceOpen.value = id;
  sourceDetail.value = {};
  try {
    const data = object(await get<Record<string, unknown>>(`/api/cloud/assets/${encodeURIComponent(id)}/source`));
    sourceDetail.value = data;
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function toggleRule(id: string, enabled: boolean) {
  try {
    await post(`/api/cloud/rules/${encodeURIComponent(id)}/enabled`, { enabled });
    await loadRules();
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function removeRule(id: string) {
  try {
    await del(`/api/cloud/rules/${encodeURIComponent(id)}`);
    await loadRules();
  } catch (e) {
    error.value = (e as Error).message;
  }
}
onMounted(async () => {
  await loadAccounts();
  await Promise.all([loadAssets(), loadRules()]);
});
</script>
<template>
  <section class="panel">
    <div class="row-between">
      <h3 data-icon="film">STRM 生成与更新</h3>
      <div style="display: flex; gap: 8px; align-items: center; flex: 0 0 auto">
        <select v-model="accountId" aria-label="115 账号" @change="loadAssets()">
          <option v-for="account in accounts" :key="text(account.id)" :value="text(account.id)">
            {{ text(account.name || account.id) }}
          </option>
        </select>
        <button class="btn" style="white-space: nowrap" :disabled="busy || !accountId" @click="sync">
          {{ busy ? '提交中…' : '立即全量扫描并生成 / 更新' }}
        </button>
        <button class="btn gray" style="white-space: nowrap" :disabled="loading" @click="loadAssets()">刷新记录</button>
      </div>
    </div>
    <p class="hint">按所选来源扫描，生成稳定播放入口。同名外部 STRM 不会被覆盖。</p>
    <p v-if="message" class="hint" role="status">{{ message }}</p>
    <p v-if="error" class="hint">{{ error }}</p>

    <div class="cw-settings-group">
      <h4>定时同步 STRM</h4>
      <div v-if="!rules.length" class="hint">暂无定时同步规则。</div>
      <div v-for="rule in rules" :key="text(rule.id)" class="cw-file-row">
        <span class="cw-file-name">{{ text(rule.name || rule.id) }}</span>
        <span class="cw-file-meta">
          {{ text(rule.interval_hours ? `每 ${rule.interval_hours} 小时` : rule.rrule || '') }}
        </span>
        <button class="btn gray sm" @click="toggleRule(text(rule.id), rule.enabled !== true)">
          {{ rule.enabled === true ? '停用' : '启用' }}
        </button>
        <button class="btn gray sm" @click="removeRule(text(rule.id))">删除</button>
      </div>
    </div>

    <div v-if="!assets.length && !loading" class="list-empty">尚未生成 STRM。</div>
    <article v-for="item in assets" :key="text(item.id)" class="panel">
      <b>{{ text(item.path) }}</b>
      <p>{{ stateLabels[text(item.state)] || text(item.state) }}</p>
      <div style="display: flex; gap: 8px; flex-wrap: wrap">
        <button class="btn gray" @click="toggleSource(text(item.id))">查看来源与删除回执</button>
        <button class="btn gray" @click="revoke(text(item.id))">撤销播放引用</button>
      </div>
      <div v-if="sourceOpen === text(item.id)" class="hint" style="margin-top: 8px">
        <template v-if="sourceDetail.status">
          <p>115 账号 {{ text(sourceDetail.uid) }} · 文件 ID {{ text(sourceDetail.file_id) }}</p>
          <p>{{ text(sourceDetail.effect || sourceDetail.blocked || '来源尚不可核实') }}</p>
          <p v-if="sourceDetail.enabled === false">Cookie 源文件删除未就绪，请核对开关与登录凭证。</p>
        </template>
        <span v-else>读取来源中…</span>
      </div>
    </article>

    <div v-if="assets.length" class="cw-file-pagination">
      <button class="btn gray" :disabled="!hasMore || loading" @click="loadAssets(true)">下一页</button>
    </div>
  </section>
</template>
