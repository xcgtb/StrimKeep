<script setup lang="ts">
import { ref, watch } from 'vue';
import { useCloud } from '../stores/cloud';
import { readData, writeData, removeData, accountPath, records, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import PageHeading from '../components/PageHeading.vue';
import AccountPicker from '../components/AccountPicker.vue';
import FormFields, { type Field } from '../components/FormFields.vue';
import DirectoryPicker from '../components/DirectoryPicker.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
const cloud = useCloud(),
  op = useOperation(() => cloud.id),
  sources = useRequest<Data>(),
  rules = useRequest<Data>(),
  selected = ref<string[]>([]),
  form = ref<Data>({
    schedule_mode: 'interval',
    interval_seconds: 3600,
    cron_expression: '0 3 * * *',
    timezone: 'Asia/Shanghai',
  }),
  custom = ref<Data>({ name: '', kind: 'workflow', enabled: false }),
  source = ref(''),
  target = ref(''),
  unknown = ref(''),
  confirmation = ref(''),
  row = ref<Data>(),
  job = ref('');
const fields: Field[] = [
  {
    key: 'schedule_mode',
    label: '触发方式',
    type: 'select',
    options: [
      { value: 'interval', label: '固定间隔' },
      { value: 'cron', label: 'Cron' },
    ],
  },
  { key: 'interval_seconds', label: '间隔（秒）', type: 'number', min: 300, max: 604800 },
  { key: 'cron_expression', label: 'Cron 表达式' },
  { key: 'timezone', label: '时区' },
];
async function load() {
  if (!cloud.id) return;
  const aid = cloud.id;
  await Promise.allSettled([
    sources.run((signal) => readData(accountPath(aid, 'organizer-sources'), signal)),
    rules.run((signal) => readData('/api/cloud/rules', signal)),
  ]);
  if (aid === cloud.id) {
    const saved = records(rules.data.value?.items).filter(
      (r) => r.account_id === aid && r.intake_rule && r.enabled,
    );
    selected.value = saved.map((r) => String(r.source_cid));
    if (saved[0])
      for (const key of fields.map((f) => f.key))
        if (key in saved[0]) form.value[key] = saved[0][key];
    op.reconcile();
  }
}
async function execute() {
  const kind = confirmation.value;
  let path = '',
    body: Data | undefined;
  if (kind === 'save') {
    path = accountPath(cloud.id, 'organizer-sources');
    body = {
      ...form.value,
      source_ids: selected.value,
      expected_revision: sources.data.value?.revision,
    };
  } else if (kind === 'custom') {
    path = '/api/cloud/rules';
    body = {
      ...form.value,
      ...custom.value,
      account_id: cloud.id,
      source_cid: source.value,
      target_cid: target.value || null,
      unknown_cid: unknown.value || null,
      overrides: {},
    };
  } else if (kind === 'stop') {
    path = accountPath(cloud.id, 'organizer/stop');
  } else if (kind === 'run') {
    path = accountPath(cloud.id, 'organizer/run');
    body = {
      expected_revision: sources.data.value?.revision,
      ...(row.value?.id ? { source_cid: String(row.value.id) } : {}),
    };
  } else if (kind === 'toggle') {
    path = `/api/cloud/rules/${row.value?.id}/enabled`;
    body = { enabled: !row.value?.enabled };
  }
  const r = await op.run(() =>
    kind === 'delete' ? removeData(`/api/cloud/rules/${row.value?.id}`) : writeData(path, body),
  );
  if (r) {
    if (r.job_id) job.value = String(r.job_id);
    confirmation.value = '';
    await load();
  }
}
watch(
  () => cloud.id,
  () => {
    sources.cancel();
    rules.cancel();
    selected.value = [];
    source.value = '';
    target.value = '';
    unknown.value = '';
    row.value = undefined;
    custom.value = { name: '', kind: 'workflow', enabled: false };
    form.value = {
      schedule_mode: 'interval',
      interval_seconds: 3600,
      cron_expression: '0 3 * * *',
      timezone: 'Asia/Shanghai',
    };
    confirmation.value = '';
    job.value = '';
    op.reconcile();
    void load();
  },
  { immediate: true },
);
</script>
<template>
  <PageHeading
    title="整理自动化"
    subtitle="复用已保存接收目录，设置整理与 STRM 同步任务。"
  /><AccountPicker />
  <nav class="context-tabs">
    <RouterLink to="/organize/workspace">整理工作区</RouterLink
    ><RouterLink to="/organize/automation">整理自动化</RouterLink>
  </nav>
  <OperationState
    :busy="op.busy.value"
    :error="op.error.value || sources.error.value || rules.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  /><template v-if="cloud.id"
    ><section class="panel">
      <h2>接收目录自动整理</h2>
      <p class="muted-text">
        归档目标：{{ (sources.data.value?.target as Data)?.name || '请先保存归档目录' }}
      </p>
      <div class="source-checkboxes">
        <label
          v-for="s in records(sources.data.value?.items)"
          :key="String(s.id)"
          class="checkbox-label"
          ><input v-model="selected" type="checkbox" :value="String(s.id)" />{{ s.name }} ·
          {{ (s.roles as string[])?.join('、') }}</label
        >
      </div>
      <FormFields v-model="form" :fields="fields" />
      <div class="action-row">
        <button
          class="button"
          @click="op.run(() => writeData('/api/cloud/schedules/preview', form))"
        >
          预览下次运行时间</button
        ><button
          class="button primary"
          :disabled="
            !selected.length ||
            !sources.data.value ||
            !!sources.error.value ||
            op.busy.value ||
            op.uncertain.value
          "
          @click="confirmation = 'save'"
        >
          保存接收目录规则</button
        ><button
          class="button"
          @click="
            row = undefined;
            confirmation = 'run';
          "
        >
          立即整理已保存来源</button
        ><button class="button danger-text" @click="confirmation = 'stop'">
          安全停止并暂停关联规则
        </button>
      </div>
    </section>
    <section class="panel">
      <header class="section-heading">
        <h2>当前账号规则</h2>
        <button class="button" @click="load">刷新并核实</button>
      </header>
      <div
        v-for="r in records(rules.data.value?.items).filter((r) => r.account_id === cloud.id)"
        :key="String(r.id)"
        class="automation-rule"
      >
        <div>
          <strong>{{ r.name }}</strong>
          <p>
            {{ r.enabled ? '已启用' : '已暂停' }} ·
            {{ r.kind === 'sync' ? 'STRM 同步' : '媒体整理' }}
          </p>
          <small
            >下次时间
            {{
              r.next_run ? new Date(Number(r.next_run) * 1000).toLocaleString('zh-CN') : '—'
            }}</small
          >
        </div>
        <button
          class="button small"
          @click="
            row = r;
            confirmation = 'toggle';
          "
        >
          {{ r.enabled ? '暂停' : '启用' }}</button
        ><button
          class="button small danger-text"
          @click="
            row = r;
            confirmation = 'delete';
          "
        >
          移除规则
        </button>
      </div>
    </section>
    <details class="panel">
      <summary>添加独立整理 / 同步规则</summary>
      <FormFields
        v-model="custom"
        :fields="[
          { key: 'name', label: '规则名称' },
          {
            key: 'kind',
            label: '任务类型',
            type: 'select',
            options: [
              { value: 'workflow', label: '媒体整理' },
              { value: 'sync', label: 'STRM 同步' },
            ],
          },
          { key: 'enabled', label: '保存后启用', type: 'check' },
        ]"
      />
      <div class="form-grid">
        <DirectoryPicker v-model="source" :account-id="cloud.id" label="来源目录" /><DirectoryPicker
          v-if="custom.kind === 'workflow'"
          v-model="target"
          :account-id="cloud.id"
          label="归档目录"
        /><DirectoryPicker
          v-if="custom.kind === 'workflow'"
          v-model="unknown"
          :account-id="cloud.id"
          label="未识别目录（可选）"
        />
      </div>
      <p class="muted-text">使用上面的触发时间配置。</p>
      <button
        class="button primary"
        :disabled="
          !custom.name ||
          !source ||
          (custom.kind === 'workflow' && !target) ||
          op.busy.value ||
          op.uncertain.value
        "
        @click="confirmation = 'custom'"
      >
        核对独立规则
      </button>
    </details></template
  ><TaskReceipt v-if="job" :id="job" /><ConfirmAction
    v-if="confirmation"
    title="确认自动化配置与操作"
    :description="
      confirmation === 'stop'
        ? '当前文件组完成后安全停止，关联定时规则会同时暂停。'
        : confirmation === 'delete'
          ? '移除这条定时规则，不删除媒体。'
          : confirmation === 'run'
            ? '对已保存来源提交一次整理任务。'
            : '按当前账号、目录与时间配置保存或切换定时规则；启用的规则会自动执行文件操作。'
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirmation = ''"
    @confirm="execute"
    ><EvidenceDetails :value="row || form"
  /></ConfirmAction>
</template>
