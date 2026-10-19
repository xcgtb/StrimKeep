<script setup lang="ts">
import { ref, watch } from 'vue';
import { date } from '../composables/format';
import { useCloud } from '../stores/cloud';
import { readData, writeData, accountPath, query, records, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import AccountPicker from '../components/AccountPicker.vue';
import PageHeading from '../components/PageHeading.vue';
import DirectoryPicker from '../components/DirectoryPicker.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
import OutputSettings from '../components/OutputSettings.vue';
import DirectoryRoles from '../components/DirectoryRoles.vue';
import PlaybackCheck from '../components/PlaybackCheck.vue';
import { useActivityUpdates } from '../composables/useActivityUpdates';
import ModalDialog from '../components/ModalDialog.vue';
const cloud = useCloud(),
  op = useOperation(() => cloud.id),
  assets = useRequest<Data>(),
  source = useRequest<Data>(),
  pipeline = useRequest<Data>(),
  schedule = useRequest<Data>(),
  cid = ref(''),
  cidName = ref(''),
  directoryConfig = useRequest<Data>(),
  offset = ref(0),
  job = ref(''),
  confirmation = ref(''),
  asset = ref<Data>(),
  sourceOpen = ref(false);
async function load() {
  if (!cloud.id) return;
  const aid = cloud.id;
  await Promise.allSettled([
    assets.run((signal) =>
      readData(
        query('/api/cloud/assets', { account_id: aid, offset: offset.value, limit: 50 }),
        signal,
      ),
    ),
    pipeline.run((signal) => readData(accountPath(aid, 'pipeline'), signal)),
    schedule.run((signal) => readData('/api/cloud/settings', signal)),
  ]);
}
async function loadDefaultSource() {
  if (!cloud.id) return;
  const aid = cloud.id;
  const result = await directoryConfig.run((signal) =>
    readData(accountPath(aid, 'directories'), signal),
  );
  if (result && aid === cloud.id) {
    const saved = object(object(result.directories).strm);
    cid.value = String(saved.id || '');
    cidName.value = String(saved.name || '');
  }
}
async function inspect(row: Data) {
  asset.value = row;
  sourceOpen.value = true;
  source.data.value = undefined;
  await source.run((signal) => readData(`/api/cloud/assets/${row.id}/source`, signal));
}
async function execute() {
  const action = confirmation.value;
  let path = '/api/cloud/strm/sync',
    body: Data | undefined = {
      account_id: cloud.id,
      source_cid: cid.value,
      confirmed: true,
      overrides: {},
      reconcile: object(schedule.data.value?.settings).strm_reconcile_enabled !== false,
    };
  if (action !== 'sync') {
    path = `/api/cloud/assets/${asset.value?.id}/${action === 'revoke' ? 'revoke' : `deletion/${action}`}`;
    body = action === 'reset' ? { confirmed: true } : undefined;
  }
  const r = await op.run(() => writeData(path, body));
  if (r) {
    if (r.job_id) job.value = String(r.job_id);
    confirmation.value = '';
    await load();
    if (asset.value && sourceOpen.value) await inspect(asset.value);
  }
}
watch(
  () => cloud.id,
  () => {
    assets.cancel();
    pipeline.cancel();
    schedule.cancel();
    source.cancel();
    directoryConfig.cancel();
    sourceOpen.value = false;
    asset.value = undefined;
    offset.value = 0;
    cid.value = '';
    cidName.value = '';
    job.value = '';
    confirmation.value = '';
    op.reconcile();
    void load();
    void loadDefaultSource();
  },
  { immediate: true },
);
useActivityUpdates(() => {
  if (!op.busy.value && !sourceOpen.value) return load();
});
useVisiblePoll(() => {
  if (!op.busy.value && !sourceOpen.value) return load();
}, 20000);
</script>
<template>
  <PageHeading
    title="STRM 资源"
    subtitle="管理已生成的播放引用、源文件关系与同步任务。"
  /><AccountPicker />
  <div class="workspace-card-stack strm-workspace">
    <section class="panel">
      <header class="section-heading">
        <h2>自动运行概况</h2>
        <button class="button small" @click="load">刷新状态</button>
      </header>
      <p v-if="schedule.error.value" class="inline-error">{{ schedule.error.value }}</p>
      <template v-if="schedule.data.value">
        <p>
          定时同步：{{ object(schedule.data.value.schedule).enabled ? '已启用' : '未启用' }} ·
          {{ object(schedule.data.value.settings).strm_scan_cron || '未设置 Cron' }}
        </p>
        <p>上次投递：{{ date(Number(object(schedule.data.value.schedule).last_run)) }}</p>
        <p class="muted-text">
          上次调度检查：{{
            date(Number(object(schedule.data.value.schedule).last_check))
          }}（已有任务时跳过）
        </p>
        <p>
          预计下次：{{
            object(schedule.data.value.schedule).enabled
              ? date(Number(object(schedule.data.value.schedule).next_run))
              : '未启用'
          }}（排队后串行执行）
        </p>
        <p>当前账号同步来源：{{ cidName || '尚未保存' }}</p>
        <p class="rule-note">
          {{
            object(schedule.data.value.schedule).cleanup_enabled
              ? '包含失效引用清理：连续两次完整扫描缺失并核验挂载、内容后，清理 NAS 自建 STRM 及未变化的自建附件。'
              : '仅补生成或更新 STRM，失效引用清理已关闭。'
          }}115 源文件不会因此被删除。
        </p>
        <RouterLink to="/tasks?kind=sync" class="text-link">查看同步任务与清理回执 →</RouterLink>
      </template>
    </section>
    <details class="module-config">
      <summary>当前配置：排期、输出、播放地址与来源</summary>
      <OutputSettings @saved="load" />
      <DirectoryRoles :roles="['strm']" title="当前账号定时同步来源" @saved="loadDefaultSource" />
    </details>
    <OperationState
      :busy="op.busy.value"
      :error="op.error.value || assets.error.value || directoryConfig.error.value"
      :uncertain="op.uncertain.value"
      :receipt="op.receipt.value"
    />
    <template v-if="cloud.id">
      <section class="panel">
        <h2>播放引用</h2>
        <div class="asset-list">
          <article v-for="row in records(assets.data.value?.items)" :key="String(row.id)">
            <div>
              <strong>{{ String(row.path).split('/').pop() }}</strong>
              <p class="path-text">{{ row.path }}</p>
              <small>{{ row.state }}</small>
            </div>
            <button class="button small" @click="inspect(row)">源关系与回执</button>
          </article>
          <p v-if="!records(assets.data.value?.items).length" class="muted-text">
            当前账号暂无播放引用。
          </p>
        </div>
        <div class="pagination">
          <button
            class="button"
            :disabled="offset === 0 || assets.loading.value"
            @click="
              offset -= 50;
              load();
            "
          >
            上一页</button
          ><span>{{ offset + 1 }} 起</span
          ><button
            class="button"
            :disabled="records(assets.data.value?.items).length < 50 || assets.loading.value"
            @click="
              offset += 50;
              load();
            "
          >
            下一页
          </button>
        </div>
      </section>
      <details class="module-config">
        <summary>手动补生成与高级核验</summary>
        <PlaybackCheck :key="cloud.id" :assets="records(assets.data.value?.items)" />
        <section class="panel">
          <header class="section-heading">
            <h2>输出与播放配置检查</h2>
            <span class="muted-text">使用已保存的输出配置核验</span>
          </header>
          <div class="readiness-grid">
            <article
              v-for="check in records(pipeline.data.value?.checks)"
              :key="String(check.key)"
              :class="{ ready: check.ready }"
            >
              <strong>{{ check.ready ? '✓' : '! ' }} {{ check.label }}</strong>
              <p>{{ check.detail }}</p>
            </article>
          </div>
          <div class="form-grid strm-sync-source">
            <DirectoryPicker
              v-model="cid"
              :name="cidName"
              :account-id="cloud.id"
              label="手动同步的 115 来源目录"
              @selected="(v) => (cidName = v.name)"
            />
            <div class="action-row">
              <button
                class="button primary"
                :disabled="!cid || op.busy.value || op.uncertain.value"
                @click="confirmation = 'sync'"
              >
                核对并同步 STRM</button
              ><button
                class="button"
                @click="
                  load();
                  op.reconcile();
                "
              >
                刷新并核实
              </button>
            </div>
          </div>
        </section>
      </details>
    </template>
    <TaskReceipt v-if="job" :id="job" />
  </div>
  <ModalDialog
    v-if="sourceOpen"
    title="播放引用与源文件关系"
    @close="
      sourceOpen = false;
      source.cancel();
    "
    ><p v-if="source.error.value" class="inline-error">{{ source.error.value }}</p>
    <EvidenceDetails :value="source.data.value" /><template #footer
      ><button
        class="button danger-text"
        :disabled="op.busy.value || op.uncertain.value"
        @click="
          sourceOpen = false;
          confirmation = 'revoke';
        "
      >
        撤销播放引用</button
      ><button
        v-if="source.data.value && object(source.data.value.source).source_kind !== 'share'"
        class="button"
        @click="
          sourceOpen = false;
          confirmation = 'verify';
        "
      >
        只读核实源删除</button
      ><button
        v-if="source.data.value && object(source.data.value.source).source_kind !== 'share'"
        class="button"
        @click="
          sourceOpen = false;
          confirmation = 'reset';
        "
      >
        重置被拒绝删除记录
      </button></template
    ></ModalDialog
  ><ConfirmAction
    v-if="confirmation"
    :title="confirmation === 'sync' ? '同步生成 STRM' : '处理播放引用'"
    :description="
      confirmation === 'sync'
        ? '读取所选目录并补生成或更新 STRM。如已启用失效引用清理，连续两次完整扫描缺失且挂载、内容核验通过的 NAS 自建引用及附件会被清理；不删除 115 文件。实际 Emby 入库另行核实。'
        : confirmation === 'revoke'
          ? '将此播放引用标记为已撤销，后续播放被拒绝；此动作本身不删除 NAS 文件或 115 源文件，也不会自动重新启用。'
          : confirmation === 'verify'
            ? '只读核实已经发出的源删除回执，不重新发出删除请求。'
            : '重置被拒绝的来源删除记录，使其可以在之后重新预览核实。'
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    :danger="confirmation === 'revoke'"
    @close="confirmation = ''"
    @confirm="execute"
    ><EvidenceDetails v-if="asset && confirmation !== 'sync'" :value="asset"
  /></ConfirmAction>
</template>
