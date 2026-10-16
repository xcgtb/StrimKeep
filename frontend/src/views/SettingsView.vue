<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { get, post, ApiError } from '../api/client';
import { readData, writeData, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useUi } from '../stores/ui';
import PageHeading from '../components/PageHeading.vue';
import FormFields, { type Field } from '../components/FormFields.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
interface ConfigResponse {
  config: Record<string, string>;
  env_overridden: string[];
  skipped_env?: string[];
}
const request = useRequest<ConfigResponse>(),
  ingest = useRequest<Data>(),
  op = useOperation(),
  ui = useUi(),
  form = ref<Data>({}),
  ingestForm = ref<Data>({}),
  saving = ref(false),
  testing = ref(''),
  result = ref(''),
  uncertain = ref(false),
  confirmation = ref('');
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
      { key: 'emby_host', label: '服务地址' },
      { key: 'emby_key', label: 'API Key', type: 'secret' },
      { key: 'emby_local_path', label: 'Emby 中的本地 STRM 路径' },
      { key: 'emby_share_path', label: 'Emby 中的分享 STRM 路径' },
    ],
  },
  {
    key: 'tmdb',
    title: 'TMDB 元数据',
    description: '影视识别、海报与已播集数',
    test: 'tmdb',
    fields: [
      { key: 'tmdb_key', label: 'API Key / Read Access Token', type: 'secret' },
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
      { key: 'telegram_bot_token', label: 'Bot Token', type: 'secret' },
      { key: 'telegram_chat_id', label: 'Chat ID' },
      { key: 'telegram_allowed_users', label: '允许使用机器人的用户 ID（按原配置分隔）' },
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
      { key: 'http_proxy_url', label: 'HTTP 代理地址' },
      { key: 'http_proxy_username', label: '代理用户名' },
      { key: 'http_proxy_password', label: '代理密码', type: 'secret' },
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
      { key: 'background_light_url', label: '浅色主题背景图片 URL' },
      { key: 'background_dark_url', label: '深色主题背景图片 URL' },
    ],
  },
];
const ingestFields: Field[] = [
  { key: 'enabled', label: '启用入库监控', type: 'check' },
  {
    key: 'mode',
    label: '监控模式',
    type: 'select',
    options: [
      { value: 'poll', label: '定时增量发现' },
      { value: 'webhook', label: 'Emby 实时回调' },
    ],
  },
  { key: 'interval_min', label: '增量轮询间隔（分钟）', type: 'number', min: 1, max: 1440 },
  { key: 'reconcile_hours', label: '全量校准间隔（小时）', type: 'number', min: 1, max: 168 },
];
const changed = computed(() =>
  Object.keys(form.value).filter(
    (k) =>
      String(form.value[k] ?? '') !== String(request.data.value?.config[k] ?? '') &&
      !request.data.value?.env_overridden.includes(k),
  ),
);
const callback = computed(() => {
  const token = request.data.value?.config.ingest_webhook_token;
  return token ? `${location.origin}/api/emby/webhook?token=${encodeURIComponent(token)}` : '';
});
function fill(response: ConfigResponse) {
  form.value = Object.fromEntries(
    groups.flatMap((g) => g.fields).map((f) => [f.key, response.config[f.key] ?? '']),
  );
}
async function load() {
  const response = await request.run((signal) => get<ConfigResponse>('/api/config', signal));
  if (response) {
    fill(response);
    uncertain.value = false;
    ui.setAppearance(response.config);
  }
  await loadIngest();
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
  result.value = '';
  try {
    const r = await post<{ message?: string }>(`/api/config/test/${service}`, form.value);
    result.value = r.message || '连接测试完成';
  } catch (e) {
    result.value = (e as Error).message;
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
</script>
<template>
  <PageHeading title="连接与设置" subtitle="集中管理服务、通知、网络、实时入库与外观，保存后生效。"
    ><button
      class="button primary"
      :disabled="saving || !!testing || !changed.length || !!request.error.value || uncertain"
      @click="save"
    >
      {{ saving ? '正在保存…' : `保存${changed.length ? ` ${changed.length} 项更改` : '设置'}` }}
    </button></PageHeading
  >
  <nav class="context-tabs">
    <RouterLink to="/settings">系统连接</RouterLink
    ><RouterLink to="/settings/accounts">115 账号与目录</RouterLink
    ><RouterLink to="/settings/cloud">整理与播放设置</RouterLink
    ><RouterLink to="/governance/rules">治理规则</RouterLink
    ><RouterLink to="/settings/external">外部服务</RouterLink>
  </nav>
  <OperationState :busy="request.loading.value" :error="request.error.value" />
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
        <FormFields
          v-model="form"
          :fields="
            group.fields.map((f) => ({
              ...f,
              disabled: request.data.value!.env_overridden.includes(f.key),
              hint: request.data.value!.env_overridden.includes(f.key)
                ? '由部署环境变量控制'
                : f.hint,
            }))
          "
          :disabled="saving || !!testing || uncertain"
        /><button
          v-if="group.test"
          class="button"
          :disabled="!!testing || saving || uncertain"
          @click="test(group.test)"
        >
          {{
            testing === group.test
              ? '正在测试…'
              : group.test === 'telegram'
                ? '发送一条测试通知'
                : '测试连接'
          }}</button
        ><button v-if="group.key === 'appearance'" class="button" @click="ui.toggleTheme">
          {{ ui.theme === 'light' ? '切换为深色' : '切换为浅色' }}
        </button>
      </section>
      <section id="settings-ingest" class="panel settings-panel">
        <h2>入库监控与 Emby 回调</h2>
        <FormFields
          v-model="ingestForm"
          :fields="ingestFields"
          :disabled="op.busy.value || op.uncertain.value"
        /><OperationState
          :error="ingest.error.value || op.error.value"
          :uncertain="op.uncertain.value"
          :receipt="op.receipt.value"
        /><label v-if="callback"
          >Emby 回调地址（含令牌，请妥善保管）<input
            :value="callback"
            type="password"
            readonly
          /><button class="button small" @click="copyCallback">复制回调地址</button></label
        >
        <div class="action-row">
          <button
            class="button primary"
            :disabled="
              !ingest.data.value || !!ingest.error.value || op.busy.value || op.uncertain.value
            "
            @click="confirmation = 'save'"
          >
            保存入库监控</button
          ><button
            v-if="!callback"
            class="button"
            :disabled="op.busy.value || op.uncertain.value"
            @click="confirmation = 'generate'"
          >
            首次生成回调令牌</button
          ><button v-else class="button danger-text" @click="confirmation = 'reset'">
            重置回调令牌</button
          ><button
            class="button"
            @click="
              load();
              op.reconcile();
            "
          >
            重新读取监控状态
          </button>
        </div>
        <EvidenceDetails :value="ingest.data.value?.webhook" />
      </section>
    </div>
  </div>
  <pre v-if="result" class="test-result" role="status">{{ result }}</pre>
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
