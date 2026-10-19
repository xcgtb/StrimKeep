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
import SectionCard from '../components/SectionCard.vue';
import { KeyRound, ShieldCheck, CalendarCheck, Trash2 } from '@lucide/vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import OperationState from '../components/OperationState.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
const cloud = useCloud(),
  op = useOperation(() => cloud.id),
  maintenance = useRequest<Data>();
const cookie = ref(''),
  device = ref('alipaymini'),
  mode = ref('cookie'),
  targetExisting = ref(false),
  flow = ref(''),
  phase = ref(''),
  maintenanceForm = ref<Data>({}),
  confirmation = ref(''),
  job = ref('');
// 每条都回答「现在能不能用」，不暴露内部字段名。
const signinFields: Field[] = [
  {
    key: 'signin_enabled',
    label: '每日自动签到',
    type: 'check',
    hint: '开启后按下面的时间自动签到，攒空间用。',
  },
  { key: 'signin_cron', label: '签到时间（Cron，五段）', hint: '例如 0 8 * * * 表示每天 08:00。' },
  { key: 'timezone', label: '时区', hint: '按这个时区解释上面的 Cron。' },
];
const recycleFields: Field[] = [
  {
    key: 'recycle_enabled',
    label: '自动清理回收站',
    type: 'check',
    hint: '按保留天数永久删除回收站里的条目，无法恢复。',
  },
  { key: 'recycle_cron', label: '清理时间（Cron，五段）', hint: '例如 0 4 * * * 表示每天 04:00。' },
  {
    key: 'retention_days',
    label: '回收站保留天数',
    type: 'number',
    min: 0,
    max: 3650,
    hint: '超过这个天数的条目会被清理。',
  },
  {
    key: 'password',
    label: '回收站安全密钥（留空保留已保存值）',
    type: 'secret',
    hint: '115 侧清理前需要校验的安全密钥。',
  },
];
const maintenanceFields: Field[] = [...signinFields, ...recycleFields];
const auth = computed(() => object(cloud.accounts.find((a) => a.id === cloud.id)?.authorization));
// 把后端字段翻成「能/不能做什么」，四行看完就懂。
const statusItems = computed(() => {
  const a = auth.value;
  const yes = (v: unknown) => v === true || v === 'true';
  return [
    {
      label: 'Cookie 登录',
      ok: yes(a.cookie),
      text: yes(a.cookie) ? '已保存凭证，可点击“检查 Cookie”核实当前状态' : '未保存登录凭证',
    },
    {
      label: '官方 OpenAPI',
      ok: yes(a.open) && a.open_state === 'ready' && Number(a.expires_at) > Date.now() / 1000,
      text: yes(a.open)
        ? '已保存授权，请检查官方授权以核实接口是否可用'
        : '未保存官方授权；转存、整理与源文件删除使用 Cookie 登录',
    },
    {
      label: '源文件删除（Cookie）',
      ok: yes(a.cookie_delete ?? a.cookie),
      text: yes(a.cookie_delete ?? a.cookie)
        ? '已保存 Cookie，可按文件 ID 删源；执行前仍需开启删除并核验文件身份'
        : '请先保存 Cookie 登录凭证，再配置源文件删除',
    },
    {
      label: '登录客户端',
      ok: !!a.device,
      text: String(a.device || '未识别'),
    },
  ];
});
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
    重新核实账号与配置
  </button>
  <div class="section-stack">
    <SectionCard
      title="115 登录凭证"
      desc="扫码自动获取，或粘贴浏览器里的完整 Cookie。拿到凭证后，转存、整理、播放就能用。"
    >
      <template #icon><KeyRound :size="17" /></template>
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
        >
        <p class="muted-text">
          这里选的是「用哪个客户端的身份去换 Cookie」，不是你在哪台设备扫码。签到与 STRM 生成推荐
          iPad 端，部分客户端类型可能不可用。
        </p>
        <label v-if="cloud.id" class="checkbox-label"
          ><input
            v-model="targetExisting"
            type="checkbox"
          />更新当前账号授权（否则连接一个新账号）</label
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
      <form class="form-stack" @submit.prevent="importCookie">
        <label
          >已有 Cookie<input v-model="cookie" type="password" autocomplete="off" required
        /></label>
        <p class="muted-text">
          从浏览器开发者工具复制完整 Cookie 字符串（含 UID、CID、SEID、KID），只提交给当前 StrimKeep
          服务。
        </p>
        <button class="button" :disabled="op.busy.value || op.uncertain.value">
          验证并保存 Cookie
        </button>
      </form>
    </SectionCard>

    <SectionCard
      v-if="cloud.id"
      title="当前账号状态"
      desc="四行看完，就知道现在能做什么、还差什么。"
    >
      <template #icon><ShieldCheck :size="17" /></template>
      <ul class="status-list">
        <li v-for="s in statusItems" :key="s.label">
          <span class="status-dot" :class="{ ok: s.ok }" aria-hidden="true" />
          <strong>{{ s.label }}</strong>
          <span class="status-text">{{ s.text }}</span>
        </li>
      </ul>
      <div class="action-row">
        <button class="button" @click="op.run(() => writeData(accountPath(cloud.id, 'check')))">
          检查 Cookie</button
        ><button
          class="button"
          @click="op.run(() => writeData(accountPath(cloud.id, 'open/check')))"
        >
          检查官方授权</button
        ><button class="button danger-text" @click="confirmation = 'disconnect'">断开账号</button>
      </div>
    </SectionCard>

    <SectionCard v-if="cloud.id" title="签到控制" desc="开启后按设定时间自动签到，用来攒空间。">
      <template #icon><CalendarCheck :size="17" /></template>
      <FormFields
        v-model="maintenanceForm"
        :fields="signinFields"
        :disabled="op.busy.value || op.uncertain.value"
      />
      <div class="action-row">
        <button class="button" @click="confirmation = 'signin'">立即签到</button>
      </div>
    </SectionCard>

    <SectionCard
      v-if="cloud.id"
      tone="danger"
      title="回收站与自动清理"
      desc="会真正永久删除回收站条目。先确认保留天数，再决定是否开启自动清理。"
    >
      <template #icon><Trash2 :size="17" /></template>
      <FormFields
        v-model="maintenanceForm"
        :fields="recycleFields"
        :disabled="op.busy.value || op.uncertain.value"
      />
      <div class="action-row">
        <button class="button danger-text" @click="confirmation = 'recycle'">
          按保留天数清理回收站
        </button>
      </div>
    </SectionCard>

    <div v-if="cloud.id" class="action-row save-row">
      <button class="button" @click="load">读取已保存配置</button
      ><button
        class="button primary"
        :disabled="!maintenance.data.value || !!maintenance.error.value"
        @click="confirmation = 'maintenance-save'"
      >
        核对并保存
      </button>
    </div>
    <p v-if="maintenance.error.value" class="inline-error">{{ maintenance.error.value }}</p>
  </div>
  <TaskReceipt v-if="job" :id="job" /><ConfirmAction
    v-if="confirmation"
    :title="confirmation === 'disconnect' ? '断开账号' : '确认维护操作'"
    :description="
      confirmation === 'disconnect'
        ? '断开会删除此账号已保存的目录配置、服务连接（包括 Emby、Telegram）、频道规则及读取进度，并停用定时规则；已有播放引用会被标记断开。重新授权后需要重新配置这些设置。已完成任务的历史记录保留。'
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

<style scoped>
.section-stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.status-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.status-list li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--subtle);
  font-size: 13px;
  line-height: 1.5;
}
.status-list strong {
  flex: 0 0 96px;
  font-weight: 600;
  color: var(--text);
}
.status-text {
  color: var(--muted);
}
.status-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 8px;
  border-radius: 50%;
  background: var(--muted);
  opacity: 0.5;
}
.status-dot.ok {
  background: var(--green);
  opacity: 1;
}
.save-row {
  position: sticky;
  bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel-solid);
  box-shadow: var(--shadow);
}
@media (max-width: 720px) {
  .status-list li {
    flex-wrap: wrap;
  }
  .status-list strong {
    flex-basis: auto;
  }
}
</style>
