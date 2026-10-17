<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { del, get, post } from '../api/client';
import { object, records, text } from '../api/domain';

const accounts = ref<Record<string, unknown>[]>([]);
const accountId = ref('');
const directories = ref<Record<string, string>>({});
const plan = ref<Record<string, unknown>[]>([]);
const rules = ref<Record<string, unknown>[]>([]);
const matches = ref<Record<string, unknown>[]>([]);
const busy = ref(false);
const message = ref('');
const error = ref('');
// 修正识别表单
const query = ref('');
const kind = ref<'movie' | 'tv'>('tv');
const season = ref('');
const episode = ref('');

const dirLabels: Record<string, string> = {
  source: '整理来源目录',
  target: '归档目标目录',
  unknown: '未识别目录',
  transfer: '转存目录',
  strm: 'STRM 来源目录',
};

async function loadAccounts() {
  const data = object(await get<Record<string, unknown>>('/api/cloud/status'));
  accounts.value = records(data.accounts);
  if (!accountId.value && accounts.value.length) accountId.value = text(accounts.value[0]!.id);
  await loadDirectories();
}
async function loadDirectories() {
  if (!accountId.value) return;
  try {
    const data = object(
      await get<Record<string, unknown>>(
        `/api/cloud/accounts/${encodeURIComponent(accountId.value)}/directories`,
      ),
    );
    directories.value = (data.directories as Record<string, string>) || {};
  } catch {
    directories.value = {};
  }
}
async function runOrganizer() {
  if (!accountId.value) return;
  busy.value = true;
  try {
    await post(`/api/cloud/accounts/${encodeURIComponent(accountId.value)}/organizer/run`, {});
    message.value = '已提交整理任务，正在后台执行';
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function stopOrganizer() {
  if (!accountId.value) return;
  await post(`/api/cloud/accounts/${encodeURIComponent(accountId.value)}/organizer/stop`, {});
  message.value = '已请求停止整理';
}
async function previewPlan() {
  busy.value = true;
  message.value = '正在识别并生成预览…';
  error.value = '';
  try {
    const data = object(await post<Record<string, unknown>>('/api/cloud/plans', { account_id: accountId.value }));
    plan.value = records(data.plan || data.items);
    message.value = `预览完成，共 ${plan.value.length} 项`;
  } catch (e) {
    error.value = (e as Error).message;
    message.value = '';
  } finally {
    busy.value = false;
  }
}
async function applyPlan() {
  busy.value = true;
  try {
    await post('/api/cloud/plans/apply', { account_id: accountId.value, confirmed: true });
    message.value = '已提交执行整理';
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function searchTmdb() {
  if (!query.value.trim()) return;
  busy.value = true;
  try {
    const data = object(
      await get<Record<string, unknown>>(
        `/api/cloud/tmdb?query=${encodeURIComponent(query.value)}&kind=${kind.value}`,
      ),
    );
    matches.value = records(data.results || data.items).slice(0, 10);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
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
async function removeRule(id: string) {
  await del(`/api/cloud/rules/${encodeURIComponent(id)}`);
  await loadRules();
}
onMounted(async () => {
  await loadAccounts();
  await loadRules();
});
</script>
<template>
  <section class="panel">
    <div class="row-between">
      <h3 data-icon="film">影视整理</h3>
      <select v-model="accountId" aria-label="115 账号" @change="loadDirectories()">
        <option v-for="account in accounts" :key="text(account.id)" :value="text(account.id)">
          {{ text(account.name || account.id) }}
        </option>
      </select>
    </div>
    <p class="hint">① 扫描来源与归档 → ② 识别、媒体信息与命名 → ③ 预览并执行 → ④ 定时整理与入库确认</p>

    <div class="cw-form-grid">
      <div v-for="(label, role) in dirLabels" :key="role" class="cw-directory-field">
        <label :for="'cw-' + role + '-cid'"
          >{{ label }}
          <input
            :id="'cw-' + role + '-cid'"
            v-model="directories[role]"
            placeholder="115 目录 ID"
          />
        </label>
      </div>
    </div>
    <p class="hint">目录 ID 可在「文件管理」里点击目录查看；保存后整理按这些目录扫描。</p>

    <div style="display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0">
      <button class="btn" :disabled="busy" @click="runOrganizer">立即整理 115 网盘</button>
      <button class="btn gray" @click="stopOrganizer">停止整理并保存</button>
      <button class="btn gray" :disabled="busy" @click="previewPlan">识别并预览（可选）</button>
      <button v-if="plan.length" class="btn danger" :disabled="busy" @click="applyPlan">
        确认执行整理
      </button>
    </div>

    <p class="hint" role="status">{{ message }}</p>
    <p v-if="error" class="hint">{{ error }}</p>

    <div v-if="plan.length" class="cw-settings-group">
      <h4>预览结果（{{ plan.length }} 项）</h4>
      <div v-for="row in plan.slice(0, 50)" :key="text(row.name || row.path)" class="cw-file-row">
        <span class="cw-file-name">{{ text(row.name || row.path) }}</span>
        <span class="cw-file-meta">{{ text(row.media_name || '') }} · {{ text(row.status || row.state || '') }}</span>
      </div>
    </div>

    <details class="cw-settings-group">
      <summary>修正作品识别</summary>
      <div class="cw-form-grid">
        <label
          >作品类别
          <select v-model="kind">
            <option value="movie">电影</option>
            <option value="tv">剧集</option>
          </select></label
        >
        <label
          >季号（特别篇填 0）
          <input v-model="season" /></label
        >
        <label
          >集号
          <input v-model="episode" /></label
        >
        <label
          >作品名称
          <input v-model="query" placeholder="搜索 TMDB 片名" /></label
        >
      </div>
      <button class="btn gray" :disabled="busy" @click="searchTmdb">搜索 TMDB</button>
      <div v-for="row in matches" :key="text(row.id)" class="cw-file-row">
        <span class="cw-file-name">{{ text(row.title || row.name) }}（{{ text(row.year) }}）</span>
        <span class="cw-file-meta">TMDB {{ text(row.id) }}</span>
      </div>
    </details>

    <details class="cw-settings-group">
      <summary>自动整理任务</summary>
      <div v-if="!rules.length" class="hint">暂无自动整理规则。</div>
      <div v-for="rule in rules" :key="text(rule.id)" class="cw-file-row">
        <span class="cw-file-name">{{ text(rule.name || rule.id) }}</span>
        <span class="cw-file-meta">{{ text(rule.interval_hours ? `每 ${rule.interval_hours} 小时` : rule.rrule || '') }}</span>
        <button class="btn gray sm" @click="removeRule(text(rule.id))">删除</button>
      </div>
    </details>
  </section>
</template>
