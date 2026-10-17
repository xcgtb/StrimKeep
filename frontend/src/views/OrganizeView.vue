<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { del, get, post } from '../api/client';
import { object, records, strings, text } from '../api/domain';
import DirectoryPicker from '../components/DirectoryPicker.vue';

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
// 整理参数（/api/cloud/settings）
const settings = ref<Record<string, unknown>>({});
const videoExt = ref('');
const metaExt = ref('');
const ignoreKw = ref('');
const ignoreRe = ref('');
const customCat = ref('');
const allCategories = ['电影', '剧集', '动漫', '纪录片', '综艺', '儿童节目', '演唱会'];

const dirLabels: Record<string, string> = {
  source: '整理来源目录',
  target: '归档目标目录',
  unknown: '未识别目录',
  transfer: '转存目录',
  strm: 'STRM 来源目录',
};

async function loadSettings() {
  try {
    const data = object(await get<Record<string, unknown>>('/api/cloud/settings'));
    settings.value = object(data.settings ?? data);
    videoExt.value = strings(settings.value.video_extensions).join(', ');
    metaExt.value = strings(settings.value.metadata_extensions).join(', ');
    ignoreKw.value = strings(settings.value.ignore_keywords).join('\n');
    ignoreRe.value = strings(settings.value.ignore_regex).join('\n');
    customCat.value = strings(settings.value.custom_categories).join('\n');
  } catch {
    settings.value = {};
  }
}
function splitList(value: string) {
  return value
    .split(/[\n,，、]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}
async function saveSettings() {
  busy.value = true;
  try {
    settings.value.video_extensions = splitList(videoExt.value);
    settings.value.metadata_extensions = splitList(metaExt.value);
    settings.value.ignore_keywords = ignoreKw.value.split('\n').map((s) => s.trim()).filter(Boolean);
    settings.value.ignore_regex = ignoreRe.value.split('\n').map((s) => s.trim()).filter(Boolean);
    settings.value.custom_categories = customCat.value.split('\n').map((s) => s.trim()).filter(Boolean);
    await post('/api/cloud/settings', settings.value);
    message.value = '整理参数已保存';
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
function toggleCategory(name: string) {
  const list = strings(settings.value.enabled_categories);
  settings.value.enabled_categories = list.includes(name)
    ? list.filter((c) => c !== name)
    : [...list, name];
}

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
  await loadSettings();
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
    <p class="hint">
      目录 ID 也可点下方浏览选择：
      <DirectoryPicker
        :account-id="accountId"
        @pick="(id, name) => { directories.source = id; }"
      />
    </p>

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

    <details class="cw-settings-group" open>
      <summary>整理参数（命名 / 分类 / 过滤 / 八维）</summary>
      <div class="cw-form-grid">
        <label>命名方式
          <select v-model="settings.naming_style">
            <option value="standard">完整媒体信息</option>
            <option value="compact">简洁 · 片名、年份和画质</option>
            <option value="original">保留原名</option>
            <option value="template">自定义模板</option>
          </select>
        </label>
        <label><input v-model="settings.rename_enabled" type="checkbox" />规范重命名</label>
        <label><input v-model="settings.classification_enabled" type="checkbox" />启用分类归档</label>
        <label><input v-model="settings.region_classification" type="checkbox" />地区二次分类</label>
        <label><input v-model="settings.year_classification" type="checkbox" />年份分类</label>
        <label><input v-model="settings.unknown_enabled" type="checkbox" />未识别移入未识别目录</label>
        <label><input v-model="settings.ffprobe_enabled" type="checkbox" />ffprobe 媒体信息提取</label>
        <label><input v-model="settings.generate_strm_enabled" type="checkbox" />整理后生成 STRM</label>
        <label><input v-model="settings.quality8_enabled" type="checkbox" />启用八维洗版</label>
      </div>
      <div class="cw-form-grid">
        <label>电影目录<input v-model="settings.movie_folder" /></label>
        <label>剧集目录<input v-model="settings.tv_folder" /></label>
        <label>八维模式
          <select v-model="settings.quality8_mode">
            <option value="preview">仅预览建议</option>
            <option value="upgrade">按策略执行洗版</option>
          </select>
        </label>
      </div>
      <div class="cw-settings-group">
        <strong>分类开关</strong>
        <div style="display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px">
          <label v-for="cat in allCategories" :key="cat" style="display: flex; gap: 4px; align-items: center">
            <input type="checkbox" :checked="strings(settings.enabled_categories).includes(cat)" @change="toggleCategory(cat)" />{{ cat }}
          </label>
        </div>
        <label style="margin-top: 6px">自定义分类（每行一个）
          <textarea v-model="customCat" rows="2" placeholder="例如：短剧"></textarea>
        </label>
      </div>
      <div class="cw-settings-group">
        <strong>文件过滤</strong>
        <div class="cw-form-grid">
          <label>视频扩展名（逗号分隔）<input v-model="videoExt" /></label>
          <label>附属文件扩展名（逗号分隔）<input v-model="metaExt" /></label>
          <label>忽略关键词（每行一个）<textarea v-model="ignoreKw" rows="2"></textarea></label>
          <label>忽略正则（每行一个）<textarea v-model="ignoreRe" rows="2"></textarea></label>
        </div>
      </div>
      <button class="btn" :disabled="busy" @click="saveSettings">保存整理参数</button>
    </details>

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
