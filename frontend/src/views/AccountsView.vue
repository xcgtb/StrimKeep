<script setup lang="ts">
import { onBeforeUnmount, ref, watch, computed } from 'vue';
import { useCloud } from '../stores/cloud';
import {
  accountPath,
  readData,
  writeData,
  removeData,
  object,
  text,
  type Data,
} from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import AccountPicker from '../components/AccountPicker.vue';
import PageHeading from '../components/PageHeading.vue';
import FormFields, { type Field } from '../components/FormFields.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import OperationState from '../components/OperationState.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
const cloud = useCloud(),
  op = useOperation(() => cloud.id),
  maintenance = useRequest<Data>();
const tab = ref('accounts'),
  cookie = ref(''),
  device = ref('alipaymini'),
  mode = ref('cookie'),
  targetExisting = ref(false),
  flow = ref(''),
  phase = ref(''),
  maintenanceForm = ref<Data>({}),
  confirmation = ref(''),
  job = ref('');
const maintenanceFields: Field[] = [
  { key: 'signin_enabled', label: '定时签到', type: 'check' },
  { key: 'signin_cron', label: '签到 Cron' },
  { key: 'recycle_enabled', label: '定时永久清理回收站', type: 'check' },
  { key: 'recycle_cron', label: '回收站清理 Cron' },
  { key: 'timezone', label: '时区' },
  { key: 'retention_days', label: '回收站保留天数', type: 'number', min: 0, max: 3650 },
  { key: 'password', label: '回收站安全密钥（留空保留已保存值）', type: 'secret' },
];
const auth = computed(() => object(cloud.accounts.find((a) => a.id === cloud.id)?.authorization));
async function load() {
  if (!cloud.id) return;
  op.reconcile();
  const aid = cloud.id;
  const m = await maintenance.run((signal) => readData(accountPath(aid, 'maintenance'), signal));
  if (m && aid === cloud.id) {
    const c = object(m.config);
    maintenanceForm.value = Object.fromEntries(
      maintenanceFields.filter((f) => f.key !== 'password').map((f) => [f.key, c[f.key]]),
    );
  }
}
async function cancelQR() {
  const id = flow.value;
  flow.value = '';
  if (id)
    try {
      await removeData(`/api/cloud/qr/${encodeURIComponent(id)}`);
    } catch {
      /* expiry already removes the flow */
    }
}
async function startQR() {
  await cancelQR();
  const r = await op.run(() =>
    writeData('/api/cloud/qr', {
      device: device.value,
      mode: mode.value,
      ...(targetExisting.value && cloud.id ? { account_id: cloud.id } : {}),
    }),
  );
  if (r) {
    flow.value = String(r.flow_id);
    phase.value = '请使用对应 115 客户端扫码并确认';
  }
}
async function poll() {
  if (!flow.value || op.busy.value || document.hidden) return;
  const id = flow.value;
  try {
    const r = await writeData(`/api/cloud/qr/${encodeURIComponent(id)}/poll`);
    if (flow.value !== id) return;
    phase.value =
      (
        {
          waiting: '等待扫码',
          scanned: '已扫码，请在客户端确认',
          connected: '已连接',
          expired: '二维码已过期',
          retry: '状态暂不可用，请稍后手动核实',
        } as Record<string, string>
      )[String(r.phase)] || text(r.phase);
    if (r.phase === 'connected') {
      flow.value = '';
      await cloud.load();
      if (r.account_id) cloud.id = String(r.account_id);
    }
    if (r.phase === 'expired') flow.value = '';
  } catch (e) {
    if (flow.value === id) {
      phase.value = (e as Error).message;
      flow.value = '';
    }
  }
}
useVisiblePoll(poll, 4000);
async function importCookie() {
  const r = await op.run(() =>
    writeData('/api/cloud/accounts/cookie', {
      cookie: cookie.value,
      ...(targetExisting.value && cloud.id ? { account_id: cloud.id } : {}),
    }),
  );
  cookie.value = '';
  if (r) {
    await cloud.load();
    cloud.id = String(r.account_id);
  }
}
async function execute() {
  const action = confirmation.value;
  let r: Data | undefined;
  if (action === 'disconnect')
    r = await op.run(() => removeData(`/api/cloud/accounts/${cloud.id}`));
  else if (action === 'maintenance-save')
    r = await op.run(() =>
      writeData(accountPath(cloud.id, 'maintenance'), {
        config: maintenanceForm.value,
        expected_revision: maintenance.data.value?.revision,
        confirmed: true,
      }),
    );
  else
    r = await op.run(() =>
      writeData(accountPath(cloud.id, 'maintenance/run'), { action, confirmed: true }),
    );
  if (r) {
    confirmation.value = '';
    if (r.job_id) job.value = String(r.job_id);
    if (action === 'disconnect') await cloud.load();
    else await load();
  }
}
watch(
  () => cloud.id,
  () => {
    void cancelQR();
    maintenance.cancel();
    cookie.value = '';
    maintenanceForm.value = {};
    confirmation.value = '';
    job.value = '';
    op.reconcile();
    void load();
  },
  { immediate: true },
);
onBeforeUnmount(() => void cancelQR());
</script>
<template>
  <PageHeading
    title="115 网盘配置"
    subtitle="连接 115 账号，管理扫码授权、签到与回收站维护。"
  /><AccountPicker />
  <nav class="browse-tabs">
    <button
      v-for="(label, key) in {
        accounts: '授权与健康',
        maintenance: '账号维护',
      }"
      :key="key"
      :class="{ active: tab === key }"
      @click="tab = key"
    >
      {{ label }}
    </button>
  </nav>
  <OperationState
    :busy="op.busy.value"
    :error="op.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  /><button
    v-if="op.uncertain.value"
    class="button"
    @click="
      cloud.load();
      load();
    "
  >
    重新核实账号与配置</button
  ><template v-if="tab === 'accounts'"
    ><div class="two-column">
      <section class="panel">
        <h2>扫码连接 / 更新授权</h2>
        <div class="form-stack">
          <label
            >授权类型<select v-model="mode">
              <option value="cookie">Cookie 扫码</option>
              <option value="open">115 OpenAPI 官方授权</option>
            </select></label
          ><label
            >客户端<select v-model="device">
              <option v-for="d in cloud.devices" :key="String(d.id)" :value="d.id">
                {{ d.name }}
              </option>
            </select></label
          ><label v-if="cloud.id" class="checkbox-label"
            ><input
              v-model="targetExisting"
              type="checkbox"
            />更新当前账号授权（否则连接新账号）</label
          ><button
            class="button primary"
            :disabled="op.busy.value || op.uncertain.value"
            @click="startQR"
          >
            获取二维码
          </button>
          <div v-if="flow" class="qr-box">
            <img :src="`/api/cloud/qr/${encodeURIComponent(flow)}/image`" alt="115 登录二维码" />
            <p>{{ phase }}</p>
            <button class="button" @click="poll">核实扫码状态</button
            ><button class="button" @click="cancelQR">取消授权</button>
          </div>
          <p v-else class="muted-text">{{ phase }}</p>
        </div>
      </section>
      <section class="panel">
        <h2>已有 Cookie 登录</h2>
        <p class="muted-text">填写完整 UID、CID、SEID，仅提交给当前 StrimKeep 服务。</p>
        <form class="form-stack" @submit.prevent="importCookie">
          <label
            >115 Cookie<input v-model="cookie" type="password" autocomplete="off" required /></label
          ><button class="button" :disabled="op.busy.value || op.uncertain.value">
            验证并保存 Cookie
          </button>
        </form>
        <template v-if="cloud.id"
          ><h3 class="detail-section-title">当前账号能力</h3>
          <EvidenceDetails :value="auth" />
          <div class="action-row">
            <button class="button" @click="op.run(() => writeData(accountPath(cloud.id, 'check')))">
              检查 Cookie</button
            ><button
              class="button"
              @click="op.run(() => writeData(accountPath(cloud.id, 'open/check')))"
            >
              检查官方授权</button
            ><button class="button danger-text" @click="confirmation = 'disconnect'">
              断开账号
            </button>
          </div></template
        >
      </section>
    </div></template
  >
  <section v-if="tab === 'maintenance' && cloud.id" class="panel">
    <h2>签到与回收站维护</h2>
    <FormFields
      v-model="maintenanceForm"
      :fields="maintenanceFields"
      :disabled="op.busy.value || op.uncertain.value"
    />
    <p v-if="maintenance.error.value" class="inline-error">{{ maintenance.error.value }}</p>
    <div class="action-row">
      <button class="button" @click="load">读取已保存配置</button
      ><button
        class="button primary"
        :disabled="!maintenance.data.value || !!maintenance.error.value"
        @click="confirmation = 'maintenance-save'"
      >
        核对并保存</button
      ><button class="button" @click="confirmation = 'signin'">立即签到</button
      ><button class="button danger-text" @click="confirmation = 'recycle'">
        按保留天数清理回收站
      </button>
    </div>
  </section>
  <TaskReceipt v-if="job" :id="job" /><ConfirmAction
    v-if="confirmation"
    :title="confirmation === 'disconnect' ? '断开账号' : '确认维护操作'"
    :description="
      confirmation === 'disconnect'
        ? '断开当前账号并停用相关定时规则，已有播放引用会被标记断开。'
        : confirmation === 'maintenance-save'
          ? '保存账号维护配置；开启回收站自动清理后，将按保留天数永久删除符合条件的条目。'
          : confirmation === 'recycle'
            ? '永久清理当前账号超过已保存保留天数的回收站条目，此操作无法恢复。'
            : '提交当前账号今日签到任务。'
    "
    :phrase="
      confirmation === 'recycle' ||
      (confirmation === 'maintenance-save' && maintenanceForm.recycle_enabled)
        ? '永久清理回收站'
        : undefined
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    :danger="confirmation !== 'signin'"
    @close="confirmation = ''"
    @confirm="execute"
  />
</template>
