<script setup lang="ts">
import { ref, watch } from 'vue';
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
import ModalDialog from '../components/ModalDialog.vue';
const cloud = useCloud(),
  op = useOperation(() => cloud.id),
  assets = useRequest<Data>(),
  source = useRequest<Data>(),
  pipeline = useRequest<Data>(),
  cid = ref(''),
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
  ]);
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
    source.cancel();
    sourceOpen.value = false;
    asset.value = undefined;
    offset.value = 0;
    cid.value = '';
    job.value = '';
    confirmation.value = '';
    op.reconcile();
    void load();
  },
  { immediate: true },
);
useVisiblePoll(() => {
  if (!op.busy.value && !sourceOpen.value) return load();
}, 20000);
</script>
<template>
  <PageHeading
    title="STRM 资源"
    subtitle="管理已生成的播放引用、源文件关系与同步任务。"
  /><AccountPicker /><OperationState
    :busy="op.busy.value"
    :error="op.error.value || assets.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  /><template v-if="cloud.id"
    ><section class="panel">
      <header class="section-heading">
        <h2>输出与播放配置检查</h2>
        <RouterLink class="text-link" to="/settings/cloud">设置输出与播放地址 →</RouterLink>
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
      <div class="form-grid">
        <DirectoryPicker v-model="cid" :account-id="cloud.id" label="手动同步的 115 来源目录" />
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
    </section></template
  ><TaskReceipt v-if="job" :id="job" /><ModalDialog
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
        ? '读取所选目录，按照已保存配置生成与核实 STRM；实际 Emby 入库另行核实。'
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
