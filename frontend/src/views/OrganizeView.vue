<script setup lang="ts">
import { ref, watch, computed } from 'vue';
import { request } from '../api/client';
import { useCloud } from '../stores/cloud';
import { readData, writeData, accountPath, object, records, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import PageHeading from '../components/PageHeading.vue';
import AccountPicker from '../components/AccountPicker.vue';
import DirectoryPicker from '../components/DirectoryPicker.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
import MediaOverride from '../components/MediaOverride.vue';
const cloud = useCloud(),
  op = useOperation(() => cloud.id),
  preview = useRequest<Data>(),
  config = useRequest<Data>();
const source = ref(''),
  target = ref(''),
  unknown = ref(''),
  sourceName = ref(''),
  targetName = ref(''),
  generate = ref(true),
  overrides = ref<Record<string, Data>>({}),
  fixing = ref<{ id: string; name: string }>(),
  plan = ref<Data>(),
  confirm = ref(''),
  job = ref('');
const candidates = computed(() => [
  ...records(plan.value?.items),
  ...records(plan.value?.skipped),
  ...records(plan.value?.unknown_items),
]);
function invalidate() {
  preview.cancel();
  plan.value = undefined;
  confirm.value = '';
  op.reconcile();
}
watch([source, target, unknown, generate], invalidate);
async function defaults() {
  if (!cloud.id) return;
  const aid = cloud.id;
  const r = await config.run((signal) => readData(accountPath(aid, 'directories'), signal));
  if (r && aid === cloud.id) {
    const d = object(r.directories);
    source.value = String(object(d.source).id || '');
    sourceName.value = String(object(d.source).name || '');
    target.value = String(object(d.target).id || '');
    targetName.value = String(object(d.target).name || '');
    unknown.value = String(object(d.unknown).id || '');
  }
}
async function load() {
  plan.value = undefined;
  const r = await preview.run((signal) =>
    request<Data>('/api/cloud/plans', {
      method: 'POST',
      timeoutMs: 120000,
      signal,
      body: {
        account_id: cloud.id,
        source_cid: source.value,
        target_cid: target.value,
        unknown_cid: unknown.value || null,
        overrides: overrides.value,
      },
    }),
  );
  if (r) plan.value = object(r.plan);
}
async function execute() {
  const action = confirm.value;
  const r = await op.run(() =>
    writeData(
      action === 'categories' ? '/api/cloud/categories/create' : '/api/cloud/plans/apply',
      action === 'categories'
        ? { account_id: cloud.id, target_cid: target.value, confirmed: true }
        : { plan_id: plan.value?.id, confirmed: true, generate: generate.value },
    ),
  );
  if (r) {
    job.value = String(r.job_id);
    confirm.value = '';
    plan.value = undefined;
  }
}
watch(
  () => cloud.id,
  () => {
    config.cancel();
    invalidate();
    source.value = '';
    target.value = '';
    unknown.value = '';
    job.value = '';
    overrides.value = {};
    sourceName.value = '';
    targetName.value = '';
    fixing.value = undefined;
    void defaults();
  },
  { immediate: true },
);
</script>
<template>
  <PageHeading
    title="媒体整理"
    subtitle="识别、纠正、命名与归档先预览，核对本批范围后执行。"
  /><AccountPicker />
  <nav class="context-tabs">
    <RouterLink to="/organize/workspace">整理工作区</RouterLink
    ><RouterLink to="/organize/automation">整理自动化</RouterLink
    ><RouterLink to="/settings/cloud">命名与分类设置</RouterLink
    ><RouterLink to="/tasks">处理记录</RouterLink>
  </nav>
  <OperationState
    :busy="preview.loading.value || op.busy.value"
    :error="preview.error.value || op.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <section v-if="cloud.id" class="panel">
    <div class="workflow-steps">
      <span>1 选择来源</span><span>2 识别预览</span><span>3 手动纠正</span><span>4 确认归档</span>
    </div>
    <div class="form-grid">
      <DirectoryPicker
        v-model="source"
        :name="sourceName"
        :account-id="cloud.id"
        label="待整理目录"
        @selected="(v) => (sourceName = v.name)"
      /><DirectoryPicker
        v-model="target"
        :name="targetName"
        :account-id="cloud.id"
        label="整理归档目录"
        @selected="(v) => (targetName = v.name)"
      /><DirectoryPicker
        v-model="unknown"
        :account-id="cloud.id"
        label="未识别目录（可选）"
      /><label class="checkbox-label"
        ><input v-model="generate" type="checkbox" />整理后生成 STRM</label
      >
    </div>
    <p v-if="config.error.value" class="inline-error">{{ config.error.value }}</p>
    <div class="action-row">
      <button class="button" @click="defaults">读取已保存目录</button
      ><button
        class="button primary"
        :disabled="
          !source ||
          !target ||
          source === target ||
          preview.loading.value ||
          op.busy.value ||
          op.uncertain.value
        "
        @click="load"
      >
        扫描并预览整理</button
      ><button
        class="button"
        :disabled="!target || op.busy.value || op.uncertain.value"
        @click="confirm = 'categories'"
      >
        按已保存分类创建目录
      </button>
    </div>
  </section>
  <section v-if="plan" class="panel preview-panel">
    <header class="section-heading">
      <div>
        <h2>本批识别结果</h2>
        <p>计划有效期 {{ plan.expires_in }} 秒，修改目录或识别后需要重新预览。</p>
      </div>
      <button
        class="button primary"
        :disabled="op.busy.value || op.uncertain.value"
        @click="confirm = 'apply'"
      >
        核对并整理
      </button>
    </header>
    <div class="plan-item-list">
      <article
        v-for="(row, i) in candidates"
        :key="String(object(row.source).id || row.file_id || i)"
      >
        <div>
          <strong>{{ object(row.source).name || row.name }}</strong>
          <p>{{ object(row.media).directory }} / {{ object(row.media).name || row.reason }}</p>
          <small v-if="row.reason" class="warning-text">{{ row.reason }}</small>
        </div>
        <button
          class="button small"
          @click="
            fixing = {
              id: String(object(row.source).id || row.file_id),
              name: String(object(row.source).name || row.name),
            }
          "
        >
          修正识别
        </button>
      </article>
    </div>
    <details>
      <summary>过滤、版本与清理建议</summary>
      <EvidenceDetails
        :value="{
          filtered: plan.filtered,
          quality8: plan.quality8,
          quality_held: plan.quality_held,
          cleanup_candidates: plan.cleanup_candidates,
        }"
      />
    </details>
  </section>
  <TaskReceipt v-if="job" :id="job" /><ConfirmAction
    v-if="confirm"
    :title="confirm === 'categories' ? '创建分类目录' : '执行整理归档'"
    :description="
      confirm === 'categories'
        ? '在当前归档目录下按已保存分类配置创建文件夹。'
        : '按本次预览对已识别资源改名并移动到归档目录，依据已保存洗版策略处理版本；生成 STRM 不等于 Emby 已确认入库。'
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirm = ''"
    @confirm="execute"
    ><EvidenceDetails
      v-if="plan"
      :value="{
        count: records(plan.items).length,
        unknown_items: plan.unknown_items,
        quality8: plan.quality8,
      }" /></ConfirmAction
  ><MediaOverride
    v-if="fixing"
    :file-id="fixing.id"
    :name="fixing.name"
    @close="fixing = undefined"
    @save="
      (v) => {
        overrides[v.id] = v.choice;
        fixing = undefined;
        invalidate();
        load();
      }
    "
  />
</template>
