<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { get, post, ApiError } from '../api/client';
import { readData, writeData, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { date } from '../composables/format';
import { useUi } from '../stores/ui';
import PageHeading from '../components/PageHeading.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import OperationState from '../components/OperationState.vue';

interface ConfigResponse {
  masked?: boolean;
  sensitive_keys?: string[];
  config: Record<string, string>;
  env_overridden: string[];
  skipped_env?: string[];
}
interface Field {
  key: string;
  label: string;
  type?: 'text' | 'secret' | 'number' | 'select';
  options?: { value: string; label: string }[];
  min?: number;
  max?: number;
  hint?: string;
  disabled?: boolean;
}

const request = useRequest<ConfigResponse>(),
  ingest = useRequest<Data>(),
  ingestStatus = useRequest<Data>(),
  bot = useRequest<Data>(),
  op = useOperation(),
  ui = useUi(),
  form = ref<Data>({}),
  ingestForm = ref<Data>({}),
  saving = ref(false),
  testing = ref(''),
  uncertain = ref(false),
  confirmation = ref(''),
  revealed = ref(new Set<string>());

const secretKeys = new Set(['emby_key', 'tmdb_key', 'telegram_bot_token', 'http_proxy_password']);
const switchOptions = [
  { value: '0', label: '关闭' },
  { value: '1', label: '开启' },
];
const groups: {
  key: string;
  title: string;
  description: string;
  fields: Field[];
  test?: string;
}[] = [
  {
    key: 'emby',
    title: 'Emby 媒体服务',
    description: '全局治理片库连接与路径映射',
    test: 'emby',
    fields: [
      { key: 'emby_host', label: 'Emby 服务器地址', hint: '例如 http://你的Emby地址:8096' },
      { key: 'emby_key', label: 'Emby API Key', type: 'secret' },
      { key: 'emby_local_path', label: 'Emby 中的本地库路径' },
      { key: 'emby_share_path', label: 'Emby 中的分享库路径' },
    ],
  },
  {
    key: 'tmdb',
    title: 'TMDB 元数据',
    description: '影视识别、海报与已播集数',
    test: 'tmdb',
    fields: [
      { key: 'tmdb_key', label: 'TMDB API Key', type: 'secret' },
      { key: 'tmdb_scan_enabled', label: '定期 TMDB 对照', type: 'select', options: switchOptions },
      {
        key: 'tmdb_scan_interval_hours',
        label: 'TMDB 对照间隔（小时）',
        type: 'number',
        min: 1,
        max: 168,
      },
    ],
  },
  {
    key: 'telegram',
    title: 'Telegram 通知',
    description: '机器人、接收位置与资源接收白名单',
    test: 'telegram',
    fields: [
      { key: 'telegram_bot_token', label: 'Telegram Bot Token', type: 'secret' },
      { key: 'telegram_chat_id', label: 'Telegram Chat ID' },
      {
        key: 'telegram_allowed_users',
        label: 'Telegram 允许用户（白名单）',
        hint: '逗号分隔的 User ID',
      },
    ],
  },
  {
    key: 'proxy',
    title: 'HTTP 网络代理',
    description: '用于外部服务请求，Emby 保持使用本地网络',
    test: 'proxy',
    fields: [
      {
        key: 'http_proxy_enabled',
        label: '启用 HTTP 代理',
        type: 'select',
        options: switchOptions,
      },
      {
        key: 'http_proxy_url',
        label: '代理地址',
        hint: '填写 NAS 能访问的 HTTP / 混合代理端口，不是 SOCKS5 端口。',
      },
      { key: 'http_proxy_username', label: '用户名（可选）' },
      { key: 'http_proxy_password', label: '密码（可选）', type: 'secret' },
    ],
  },
  {
    key: 'governance',
    title: '治理静默期',
    description: '入库后一定时间内先不进入治理计划',
    fields: [
      {
        key: 'ingest_quiet_minutes',
        label: '入库静默期（分钟，0 为关闭）',
        type: 'number',
        min: 0,
        max: 1440,
      },
    ],
  },
  {
    key: 'appearance',
    title: '外观背景',
    description: '图片直链留空使用默认天空蓝磨砂背景',
    fields: [
      {
        key: 'background_light_url',
        label: '浅色主题背景图片 URL',
        hint: '填写图床的图片直链，留空使用内置背景',
      },
      { key: 'background_dark_url', label: '深色主题背景图片 URL' },
    ],
  },
];
const disabled = computed(() => saving.value || !!testing.value || uncertain.value);
const changed = computed(() =>
  Object.keys(form.value).filter((key) => {
    if (request.data.value?.env_overridden.includes(key)) return false;
    if (secretKeys.has(key)) return String(form.value[key] || '').length > 0;
    return String(form.value[key] ?? '') !== String(request.data.value?.config[key] ?? '');
  }),
);
const callback = computed(() => {
  const token = request.data.value?.config.ingest_webhook_token;
  return token ? `${location.origin}/api/emby/webhook?token=${encodeURIComponent(token)}` : '';
});
const testResults = ref<Record<string, string>>({});
const botView = computed(() => {
  const b = object(bot.data.value?.bot);
  const st = b.state || (b.running ? 'ok' : 'stopped');
  const map: Record<string, [string, string]> = {
    ok: ['on', 'Bot 运行中'],
    starting: ['warn', 'Bot 启动中'],
    degraded: ['warn', 'Bot 网络不稳定，正在重试'],
    down: ['bad', 'Bot 无响应'],
    stopped: ['off', 'Bot 未运行'],
  };
  const m = map[String(st)] || map.stopped;
  return {
    dot: m[0],
    label: m[1] + (b.bot_username ? ' @' + b.bot_username : ''),
    last_poll_ago: b.last_poll_ago,
    last_error: b.last_error,
    fail_count: b.fail_count,
  };
});

function hasSecret(key: string): boolean {
  const value = String(request.data.value?.config?.[key] || '');
  return !!value && (value.includes('****') || /^\*+$/.test(value));
}
function visibleFields(fields: Field[]): Field[] {
  return fields.map((field) => {
    const controlled = !!request.data.value?.env_overridden.includes(field.key);
    const secretNote =
      field.type === 'secret'
        ? `${hasSecret(field.key) ? '✓ 已保存凭据' : '尚未配置'}；留空不会覆盖原密钥。`
        : '';
    return {
      ...field,
      disabled: controlled || field.disabled,
      hint: controlled
        ? '由 Docker/YAML 环境变量控制，请在部署配置中修改'
        : [secretNote, field.hint].filter(Boolean).join(' '),
    };
  });
}
const isSwitchField = (f: Field) =>
  f.type === 'select' &&
  f.options?.length === 2 &&
  f.options.every((o) => o.value === '0' || o.value === '1');
const isSwitchOn = (f: Field) => String(form.value[f.key] ?? '0') === '1';
function inputValue(field: Field): string {
  if (field.type === 'number')
    return form.value[field.key] == null ? '' : String(form.value[field.key]);
  return String(form.value[field.key] ?? '');
}
function onFieldInput(field: Field, event: Event) {
  const value = (event.target as HTMLInputElement).value;
  form.value = {
    ...form.value,
    [field.key]: field.type === 'number' ? (value === '' ? null : Number(value)) : value,
  };
}
function onSwitchField(field: Field, event: Event) {
  const on = (event.target as HTMLInputElement).checked;
  form.value = { ...form.value, [field.key]: on ? '1' : '0' };
}
async function toggleReveal(key: string) {
  if (revealed.value.has(key)) {
    const next = new Set(revealed.value);
    next.delete(key);
    revealed.value = next;
    form.value = { ...form.value, [key]: '' };
    return;
  }
  if (!confirm('⚠️ 即将显示完整密钥，请确保周围环境安全。继续？')) return;
  try {
    const r = await get<ConfigResponse>('/api/config?reveal=1');
    form.value = { ...form.value, [key]: r.config[key] || '' };
    const next = new Set(revealed.value);
    next.add(key);
    revealed.value = next;
    setTimeout(() => {
      if (revealed.value.has(key)) {
        const s = new Set(revealed.value);
        s.delete(key);
        revealed.value = s;
        form.value = { ...form.value, [key]: '' };
      }
    }, 60000);
  } catch (e) {
    ui.notify((e as Error).message, 'error');
  }
}

function fill(response: ConfigResponse) {
  form.value = Object.fromEntries(
    groups
      .flatMap((g) => g.fields)
      .map((f) => [f.key, secretKeys.has(f.key) ? '' : (response.config[f.key] ?? '')]),
  );
}
async function load() {
  const response = await request.run((signal) => get<ConfigResponse>('/api/config', signal));
  if (response) {
    fill(response);
    uncertain.value = false;
    ui.setAppearance(response.config);
  }
  await Promise.allSettled([loadIngest(), loadIngestStatus(), loadBotStatus()]);
}
function loadIngestStatus() {
  return ingestStatus.run((signal) => readData('/api/ingest/status', signal));
}
function loadBotStatus() {
  return bot.run((signal) => readData('/api/bot/status', signal));
}
async function loadIngest() {
  const r = await ingest.run((signal) => readData('/api/ingest/settings', signal));
  if (r) {
    const cfg = object(r.settings);
    ingestForm.value = {
      enabled: !!cfg.enabled,
      mode: cfg.mode || 'poll',
      interval_min: cfg.interval_min || 5,
      reconcile_hours: cfg.reconcile_hours || 24,
    };
  }
}
async function save() {
  saving.value = true;
  try {
    const r = await post<ConfigResponse>(
      '/api/config',
      Object.fromEntries(changed.value.map((k) => [k, form.value[k]])),
    );
    request.data.value = r;
    fill(r);
    ui.setAppearance(r.config);
    ui.notify(
      r.skipped_env?.length ? '部分配置由环境变量控制，请核对。' : '连接设置已保存',
      'success',
    );
  } catch (e) {
    uncertain.value = e instanceof ApiError && e.uncertain;
    ui.notify((e as Error).message, 'error');
  } finally {
    saving.value = false;
  }
}
async function test(service: string) {
  testing.value = service;
  testResults.value = { ...testResults.value, [service]: '正在调用后端测试接口，请稍候…' };
  try {
    const body = Object.fromEntries(
      Object.entries(form.value).filter(
        ([key, value]) => !secretKeys.has(key) || String(value || '').trim().length > 0,
      ),
    );
    const r = await post<{ message?: string }>(`/api/config/test/${service}`, body);
    testResults.value = { ...testResults.value, [service]: r.message || '连接测试完成' };
  } catch (e) {
    testResults.value = { ...testResults.value, [service]: (e as Error).message };
  } finally {
    testing.value = '';
  }
}
async function ingestAction() {
  const action = confirmation.value;
  const r = await op.run(() =>
    writeData(
      '/api/ingest/settings',
      action === 'reset'
        ? { reset_token: true, confirm_reset: 'RESET_WEBHOOK_TOKEN' }
        : action === 'generate'
          ? { generate_token: true }
          : ingestForm.value,
    ),
  );
  if (r) {
    confirmation.value = '';
    await load();
  }
}
async function copyCallback() {
  try {
    await navigator.clipboard.writeText(callback.value);
    ui.notify('回调地址已复制，请在 Emby 中配置。');
  } catch {
    ui.notify('当前浏览器无法复制，请使用安全 HTTPS 地址或手动复制。', 'error');
  }
}
function scrollTo(key: string) {
  document.getElementById(`settings-${key}`)?.scrollIntoView({ behavior: 'smooth' });
}
onMounted(load);
useVisiblePoll(async () => {
  await Promise.allSettled([
    loadIngestStatus(),
    loadBotStatus(),
    ingest.run((signal) => readData('/api/ingest/settings', signal)),
  ]);
}, 15000);
</script>
<template>
  <PageHeading title="连接与设置" subtitle="集中管理服务、通知、网络、实时入库与外观，保存后生效。"
    ><button
      class="btn"
      :disabled="saving || !!testing || !changed.length || !!request.error.value || uncertain"
      @click="save"
    >
      {{ saving ? '正在保存…' : `保存${changed.length ? ` ${changed.length} 项更改` : '设置'}` }}
    </button></PageHeading
  >
  <OperationState :busy="request.loading.value" :error="request.error.value" />
  <p v-if="request.data.value?.env_overridden.length" class="notice-bar">
    部分连接参数由 Docker/YAML 环境变量控制，已锁定相关输入项；界面显示不代表凭据丢失。
  </p>
  <p v-if="uncertain" class="notice-bar">
    保存结果待核实。<button class="text-link" @click="load">重新读取已保存配置</button>
  </p>
  <div v-if="request.data.value" class="settings-layout">
    <aside class="settings-index">
      <p class="settings-nav-caption">连接与偏好</p>
      <button v-for="group in groups" :key="group.key" @click="scrollTo(group.key)">
        {{ group.title }}</button
      ><button @click="scrollTo('ingest')">入库监控与回调</button>
    </aside>
    <div class="settings-content">
      <section
        v-for="group in groups"
        :id="`settings-${group.key}`"
        :key="group.key"
        class="panel settings-panel"
      >
        <header class="section-heading">
          <div>
            <h2>{{ group.title }}</h2>
            <p>{{ group.description }}</p>
          </div>
        </header>
        <div :class="{ 'proxy-settings': group.key === 'proxy' }">
          <template v-for="field in visibleFields(group.fields)" :key="field.key">
            <div v-if="isSwitchField(field)" class="toggle-row">
              <div>
                <div class="lbl">{{ field.label }}</div>
                <div v-if="field.hint" class="desc">{{ field.hint }}</div>
              </div>
              <label class="switch"
                ><input
                  type="checkbox"
                  :checked="isSwitchOn(field)"
                  :disabled="disabled || field.disabled"
                  @change="onSwitchField(field, $event)" /><span class="slider"></span
              ></label>
            </div>
            <div v-else class="form-row">
              <label>{{ field.label }}</label>
              <div v-if="field.type === 'secret'" class="input-row">
                <input
                  :type="revealed.has(field.key) ? 'text' : 'password'"
                  :value="inputValue(field)"
                  autocomplete="off"
                  :disabled="disabled || field.disabled"
                  @input="onFieldInput(field, $event)"
                /><button
                  class="eye-btn"
                  :class="{ on: revealed.has(field.key) }"
                  type="button"
                  @click="toggleReveal(field.key)"
                >
                  {{ revealed.has(field.key) ? '隐藏' : '查看' }}
                </button>
              </div>
              <input
                v-else
                :type="field.type === 'number' ? 'number' : 'text'"
                :value="inputValue(field)"
                :min="field.min"
                :max="field.max"
                autocomplete="off"
                spellcheck="false"
                :disabled="disabled || field.disabled"
                @input="onFieldInput(field, $event)"
              />
              <div v-if="field.hint" class="hint">{{ field.hint }}</div>
            </div>
          </template>
        </div>
        <div v-if="group.test" class="btn-row">
          <button
            class="btn gray"
            :disabled="!!testing || saving || uncertain"
            @click="test(group.test)"
          >
            {{
              testing === group.test
                ? '正在测试…'
                : group.test === 'telegram'
                  ? '发送一条测试通知'
                  : group.test === 'proxy'
                    ? '测试代理连接'
                    : '测试连接'
            }}
          </button>
        </div>
        <p v-if="group.test && testResults[group.test]" class="hint test-result" role="status">
          {{ testResults[group.test] }}
        </p>
        <div v-if="group.key === 'appearance'" class="btn-row">
          <button class="btn gray" @click="ui.toggleTheme">
            {{ ui.theme === 'light' ? '切换为深色' : '切换为浅色' }}
          </button>
        </div>
      </section>

      <section class="panel settings-panel">
        <header class="section-heading">
          <div>
            <h2>Bot 状态</h2>
            <p>Telegram 机器人连接状态，自动轮询刷新</p>
          </div>
        </header>
        <div class="bot-status">
          <span class="dot" :class="botView.dot"></span><span>{{ botView.label }}</span>
        </div>
        <p class="hint">
          上次轮询: {{ botView.last_poll_ago != null ? botView.last_poll_ago + ' 秒前' : '—'
          }}<template v-if="botView.last_error">
            · ⚠️ {{ botView.last_error
            }}<template v-if="Number(botView.fail_count) > 1"
              >（连续 {{ botView.fail_count }} 次）</template
            ></template
          >
        </p>
      </section>

      <section id="settings-ingest" class="panel settings-panel">
        <header class="section-heading">
          <div>
            <h2>入库监控与 Emby 回调</h2>
            <p>实时回调同步总览、探索入库角标和片库局部对照；全量校准负责补漏。</p>
          </div>
        </header>
        <div class="toggle-row">
          <div>
            <div class="lbl">启用后台入库监控</div>
            <div class="desc">关闭后停止自动接收入库事件及入库检查</div>
          </div>
          <label class="switch"
            ><input
              v-model="ingestForm.enabled"
              type="checkbox"
              :disabled="op.busy.value || op.uncertain.value" /><span class="slider"></span
          ></label>
        </div>
        <div class="toggle-row">
          <div>
            <div class="lbl">同步方式</div>
            <div class="desc">先配置并测试 Emby 回调，再切换实时模式</div>
          </div>
          <select v-model="ingestForm.mode" :disabled="op.busy.value || op.uncertain.value">
            <option value="poll">定时增量（兼容）</option>
            <option value="webhook">Emby 实时回调</option>
          </select>
        </div>
        <div class="toggle-row" v-show="ingestForm.mode !== 'webhook'">
          <div>
            <div class="lbl">刷新间隔（分钟）</div>
            <div class="desc">建议 5 分钟</div>
          </div>
          <div class="num-field">
            <input
              v-model.number="ingestForm.interval_min"
              type="number"
              min="1"
              max="1440"
              :disabled="op.busy.value || op.uncertain.value"
            />
          </div>
        </div>
        <div class="toggle-row">
          <div>
            <div class="lbl">全量兜底间隔（小时）</div>
            <div class="desc">实时模式默认 24 小时，校准 STRM 数量、Emby 事实及 TMDB 对照</div>
          </div>
          <div class="num-field">
            <input
              v-model.number="ingestForm.reconcile_hours"
              type="number"
              min="1"
              max="168"
              :disabled="op.busy.value || op.uncertain.value"
            />
          </div>
        </div>

        <div class="form-row">
          <label>Emby 回调地址</label>
          <input v-if="callback" :value="callback" type="password" readonly />
          <p v-else class="hint">尚未生成回调令牌</p>
          <div class="btn-row">
            <button
              v-if="!callback"
              class="btn gray"
              :disabled="op.busy.value || op.uncertain.value"
              @click="confirmation = 'generate'"
            >
              首次生成回调地址
            </button>
            <template v-else>
              <button class="btn gray" @click="copyCallback">复制回调地址</button>
              <button class="btn red" @click="confirmation = 'reset'">重置回调令牌</button>
            </template>
          </div>
        </div>

        <OperationState
          :error="ingest.error.value || op.error.value"
          :uncertain="op.uncertain.value"
          :receipt="op.receipt.value"
        />
        <section class="ingest-monitor-status">
          <h3 class="detail-section-title">监控状态与最近入库缓存</h3>
          <p v-if="ingestStatus.error.value" class="inline-error">{{ ingestStatus.error.value }}</p>
          <p v-else-if="ingestStatus.data.value?.has_cache">
            最近更新 {{ date(Number(ingestStatus.data.value.ts)) }} · 距今
            {{ ingestStatus.data.value.age_sec }} 秒
          </p>
          <p v-else class="hint">尚无入库统计缓存。</p>
          <p v-if="ingestStatus.data.value?.warning" class="notice-bar">
            {{ ingestStatus.data.value.warning }}
          </p>
        </section>
        <div class="btn-row">
          <button
            class="btn"
            :disabled="
              !ingest.data.value || !!ingest.error.value || op.busy.value || op.uncertain.value
            "
            @click="confirmation = 'save'"
          >
            保存入库监控
          </button>
          <button
            class="btn gray"
            @click="
              load();
              op.reconcile();
            "
          >
            重新读取监控状态
          </button>
        </div>
      </section>
    </div>
  </div>
  <ConfirmAction
    v-if="confirmation"
    title="确认入库监控配置"
    :description="
      confirmation === 'reset'
        ? '重置后原 Emby 回调地址立即失效，需要重新配置到 Emby。'
        : confirmation === 'generate'
          ? '首次生成当前服务的 Emby 回调令牌，仅保存在服务器。'
          : '按当前开关、模式与校准周期保存；实时模式需要先在 Emby 配置回调地址。'
    "
    :phrase="confirmation === 'reset' ? 'RESET_WEBHOOK_TOKEN' : undefined"
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirmation = ''"
    @confirm="ingestAction"
  />
</template>

<style scoped>
.form-row {
  margin: 12px 0;
}
.form-row > label {
  display: block;
  font-size: 12px;
  color: var(--text-mid);
  margin-bottom: 6px;
  font-weight: 600;
}
.form-row input,
.form-row select {
  width: 100%;
  padding: 9px 11px;
  border-radius: 10px;
  border: 1px solid var(--border-input);
  background: var(--surface-solid);
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
}
.form-row input:disabled,
.form-row select:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.hint {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 5px;
  line-height: 1.6;
}
.test-result {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-mid);
}
.input-row {
  display: flex;
  gap: 8px;
  align-items: stretch;
}
.input-row input {
  flex: 1;
}
.eye-btn {
  flex-shrink: 0;
  padding: 0 12px;
  border: 1px solid var(--border-input);
  border-radius: 10px;
  background: var(--surface-hover);
  color: var(--text-mid);
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
  font-weight: 600;
}
.eye-btn.on {
  color: var(--brand);
  border-color: var(--brand);
}
.proxy-settings {
  border: 1px dashed var(--border-strong);
  border-radius: 12px;
  padding: 4px 14px 12px;
  margin-top: 6px;
}
.btn-row {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  flex-wrap: wrap;
}
.num-field {
  width: 100px;
  flex-shrink: 0;
}
.num-field input {
  width: 100%;
}
.ingest-monitor-status {
  margin-top: 14px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.detail-section-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--text-mid);
  margin-bottom: 6px;
}
</style>
