<script setup lang="ts">
import { ref, watch } from 'vue';
import { useCloud } from '../stores/cloud';
import { readData, writeData, accountPath, records, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { date } from '../composables/format';
import PageHeading from '../components/PageHeading.vue';
import AccountPicker from '../components/AccountPicker.vue';
import FormFields, { type Field } from '../components/FormFields.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import OperationState from '../components/OperationState.vue';
const cloud = useCloud(),
  op = useOperation(() => cloud.id),
  connections = useRequest<Data>(),
  history = useRequest<Data>(),
  form = ref<Data>(),
  tab = ref('receipts'),
  confirmation = ref(''),
  selected = ref<Data>();
const fields: Field[] = [
  { key: 'name', label: '连接名称' },
  { key: 'url', label: 'Emby 服务地址' },
  { key: 'key', label: 'API Key（留空保留已保存值）', type: 'secret' },
  { key: 'personal_root', label: 'Emby 个人 STRM 根目录' },
  { key: 'share_root', label: 'Emby 分享 STRM 根目录' },
  { key: 'enabled', label: '启用此连接', type: 'check' },
  { key: 'notify', label: '此连接允许通知', type: 'check' },
];
const states: Record<string, string> = {
  pending: '等待核实',
  submitted: '已提交扫描',
  refresh_pending: '扫描待重试',
  confirmed: '已确认入库',
  timeout: '核实超时',
  paused: '已暂停',
  unverified: '尚未登记核实',
  generated: '已生成 STRM · 待入库',
  failed: '核实失败',
};
async function load() {
  if (!cloud.id) return;
  const aid = cloud.id;
  await Promise.allSettled([
    connections.run((signal) => readData(accountPath(aid, 'emby-connections'), signal)),
    history.run((signal) => readData(accountPath(aid, 'ingestion'), signal)),
  ]);
}
function edit(row?: Data) {
  form.value = row
    ? Object.fromEntries(
        ['id', 'name', 'url', 'personal_root', 'share_root', 'enabled', 'notify']
          .filter((k) => k in row)
          .map((k) => [k, row[k]]),
      )
    : {
        name: '',
        url: '',
        key: '',
        personal_root: '',
        share_root: '',
        enabled: false,
        notify: true,
      };
}
async function save() {
  if (!form.value) return;
  const r = await op.run(() =>
    writeData(accountPath(cloud.id, 'emby-connections'), {
      connection: form.value,
      expected_revision: connections.data.value?.revision,
    }),
  );
  if (form.value) form.value.key = '';
  if (r) {
    connections.data.value = r;
    form.value = undefined;
  }
}
async function execute() {
  let r: Data | undefined;
  if (confirmation.value === 'delete')
    r = await op.run(() =>
      writeData(accountPath(cloud.id, 'emby-connections'), {
        connection: { id: selected.value?.id },
        expected_revision: connections.data.value?.revision,
        delete: true,
      }),
    );
  else if (confirmation.value === 'refresh')
    r = await op.run(() => writeData('/api/cloud/emby/refresh'));
  else
    r = await op.run(() =>
      writeData(accountPath(cloud.id, `ingestion/${selected.value?.id}/retry`)),
    );
  if (r) {
    confirmation.value = '';
    await load();
  }
}
watch(
  () => cloud.id,
  () => {
    connections.cancel();
    history.cancel();
    op.reconcile();
    form.value = undefined;
    confirmation.value = '';
    void load();
  },
  { immediate: true },
);
useVisiblePoll(() => {
  if (cloud.id && !form.value && !op.busy.value)
    return history.run((signal) => readData(accountPath(cloud.id, 'ingestion'), signal));
}, 10000);
</script>
<template>
  <PageHeading
    title="Emby 入库跟踪"
    subtitle="区分 STRM 生成、扫描请求与实际入库确认，保留失败证据。"
  /><AccountPicker />
  <nav class="browse-tabs">
    <button :class="{ active: tab === 'receipts' }" @click="tab = 'receipts'">入库回执</button
    ><button :class="{ active: tab === 'connections' }" @click="tab = 'connections'">
      独立入库连接</button
    ><button
      class="text-link push-right"
      @click="
        load();
        op.reconcile();
      "
    >
      刷新并核实
    </button>
  </nav>
  <OperationState
    :busy="op.busy.value"
    :error="op.error.value || connections.error.value || history.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  /><template v-if="cloud.id"
    ><section v-if="tab === 'receipts'" class="panel">
      <header class="section-heading">
        <h2>当前账号最近 100 条记录</h2>
        <button class="button" @click="confirmation = 'refresh'">请求全局 Emby 刷新</button>
      </header>
      <div
        v-for="row in records(history.data.value?.items)"
        :key="String(row.id || row.asset_id)"
        class="ingestion-row"
      >
        <header>
          <h3>{{ row.title || '未命名作品' }}</h3>
          <span
            class="state-pill"
            :class="{ success: row.state === 'confirmed', needs_review: row.state !== 'confirmed' }"
            >{{ states[String(row.state)] || row.state }}</span
          >
        </header>
        <p class="path-text">{{ row.path }}</p>
        <p class="muted-text">{{ row.connection_name }} · {{ date(Number(row.updated)) }}</p>
        <p v-if="row.current_configuration === false" class="notice-bar">
          此回执来自旧连接设置，仅作历史参考。
        </p>
        <small class="muted-text"
          >核实尝试 {{ row.attempts ?? 0 }} 次 · 生成 STRM 不等于 Emby 已入库</small
        >
        <details v-if="row.result" class="ingestion-evidence">
          <summary>查看核实依据与原始回执</summary>
          <EvidenceDetails :value="row.result" />
        </details>
        <button
          v-if="row.id && ['timeout', 'paused'].includes(String(row.state))"
          class="button small"
          @click="
            selected = row;
            confirmation = 'retry';
          "
        >
          重新安排入库核实
        </button>
      </div>
      <p v-if="!records(history.data.value?.items).length" class="muted-text">
        暂无入库回执；先生成 STRM 并启用映射正确的入库连接。
      </p>
    </section>
    <template v-if="tab === 'connections'"
      ><button class="button primary" @click="edit()">添加入库连接</button>
      <div class="module-grid">
        <article
          v-for="c in records(connections.data.value?.items)"
          :key="String(c.id)"
          class="panel module-card"
        >
          <h2>{{ c.name }}</h2>
          <p>{{ c.url }}</p>
          <span class="small-status"
            >{{ c.enabled ? '已启用' : '已暂停' }} ·
            {{ c.key_saved ? '已保存密钥' : '未配置密钥' }}</span
          >
          <div class="action-row">
            <button class="button small" @click="edit(c)">编辑</button
            ><button
              class="button small"
              @click="
                op.run(() => writeData(accountPath(cloud.id, `emby-connections/${c.id}/test`)))
              "
            >
              测试连接</button
            ><button
              class="button small danger-text"
              @click="
                selected = c;
                confirmation = 'delete';
              "
            >
              移除
            </button>
          </div>
        </article>
      </div>
      <section v-if="form" class="panel">
        <h2>编辑入库连接</h2>
        <p class="muted-text">
          已有 API Key 不回显原文；编辑时留空保留，只有主动输入新 Key 才替换原值。连接与 STRM
          映射保存后才生效。
        </p>
        <FormFields
          v-model="form"
          :fields="fields"
          :disabled="op.busy.value || op.uncertain.value"
        />
        <div class="action-row">
          <button class="button" @click="form = undefined">取消</button
          ><button
            class="button primary"
            :disabled="
              op.busy.value ||
              op.uncertain.value ||
              !connections.data.value ||
              !!connections.error.value
            "
            @click="save"
          >
            保存连接与映射
          </button>
        </div>
      </section></template
    ></template
  ><ConfirmAction
    v-if="confirmation"
    title="确认 Emby 联动操作"
    :description="
      confirmation === 'delete'
        ? '移除当前入库连接，不删除媒体文件。'
        : confirmation === 'refresh'
          ? '提交一次当前已配置 Emby 服务的刷新请求；请求成功不表示已实际入库。'
          : '重新安排此 STRM 的 Emby 入库核实与必要的刷新请求。'
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirmation = ''"
    @confirm="execute"
  />
</template>
