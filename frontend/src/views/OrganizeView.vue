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
  config = useRequest<Data>(),
  pipeline = useRequest<Data>();
const source = ref(''),
  target = ref(''),
  unknown = ref(''),
  sourceName = ref(''),
  targetName = ref(''),
  unknownName = ref(''),
  generate = ref(true),
  overrides = ref<Record<string, Data>>({}),
  fixing = ref<{ id: string; name: string }>(),
  plan = ref<Data>(),
  confirm = ref(''),
  job = ref('');
const readinessChecks = computed(() => records(pipeline.data.value?.checks));
async function loadReadiness() {
  if (!cloud.id) return;
  const aid = cloud.id;
  await pipeline.run((signal) => readData(accountPath(aid, 'pipeline'), signal));
}
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
    unknownName.value = String(object(d.unknown).name || '');
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
    pipeline.cancel();
    invalidate();
    source.value = '';
    target.value = '';
    unknown.value = '';
    job.value = '';
    overrides.value = {};
    sourceName.value = '';
    targetName.value = '';
    unknownName.value = '';
    fixing.value = undefined;
    void defaults();
    void loadReadiness();
  },
  { immediate: true },
);
</script>
<template>
  <PageHeading
    title="媒体整理"
    subtitle="识别、纠正、命名与归档先预览，核对本批范围后执行。"
  /><AccountPicker />
  <OperationState
    :busy="preview.loading.value || op.busy.value"
    :error="preview.error.value || op.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <section v-if="cloud.id" class="panel organize-readiness" aria-label="一条龙运行前检查">
    <header class="section-heading">
      <div>
        <h2>一条龙运行前检查</h2>
        <p class="muted-text">统一核对接收目录、TMDB、归档、STRM输出与Emby入库配置。</p>
      </div>
      <button class="button small" :disabled="pipeline.loading.value" @click="loadReadiness">重新检查</button>
    </header>
    <p v-if="pipeline.error.value" class="inline-error">{{ pipeline.error.value }}</p>
    <div class="organize-readiness-grid">
      <article v-for="item in readinessChecks" :key="String(item.key)" class="organize-readiness-step">
        <span class="state-pill" :class="{ success: item.ready, needs_review: !item.ready }">{{ item.ready ? '已配置' : '需完善' }}</span>
        <strong>{{ item.label }}</strong>
        <small>{{ item.detail }}</small>
      </article>
    </div>
    <p class="gov-scan-notice" role="status">{{ pipeline.data.value?.ready ? '配置初检通过；尚须验证115授权、播放与Emby真实入库。' : '自动入库尚未准备完整，请先补齐配置再启用定时整理。' }}</p>
    <nav class="organize-workspace-links" aria-label="影视整理内部功能">
      <RouterLink to="/organize/naming">识别与命名</RouterLink>
      <RouterLink to="/settings/cloud?section=classification">归档分类</RouterLink>
      <RouterLink to="/settings/cloud?section=filters">文件过滤</RouterLink>
      <RouterLink to="/organize/quality">八维洗版</RouterLink>
      <RouterLink to="/settings/cloud?section=output">STRM与播放</RouterLink>
      <RouterLink to="/organize/automation">自动整理</RouterLink>
      <RouterLink to="/media/strm">STRM资源与同步</RouterLink>
      <RouterLink to="/tasks/ingestion">Emby入库核验</RouterLink>
    </nav>
  </section>
  <div v-if="cloud.id" class="organize-layout">
    <section class="panel organize-main">
      <header class="section-heading">
        <div>
          <h2>开始一次整理</h2>
          <p>选好来源和归档位置，先看预览再执行。</p>
        </div>
        <span class="module-tag">手动整理</span>
      </header>
      <ol class="workflow-steps">
        <li
          v-for="(step, i) in [
            { name: '选择来源', note: '设置目录' },
            { name: '识别预览', note: '核对命名' },
            { name: '修正识别', note: '调整作品' },
            { name: '确认归档', note: '提交任务' },
          ]"
          :key="step.name"
          :class="{ active: i === (plan ? 1 : 0) }"
        >
          <span>{{ i + 1 }}</span>
          <div>
            <strong>{{ step.name }}</strong
            ><small>{{ step.note }}</small>
          </div>
        </li>
      </ol>
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
          :name="unknownName"
          @selected="(v) => (unknownName = v.name)"
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
    <aside class="organize-guide">
      <div class="panel">
        <span class="eyebrow">整理前核对</span>
        <h2>让归档符合你的习惯</h2>
        <p>命名、分类和输出位置统一配置，当前任务使用已保存的规则。</p>
        <RouterLink :to="'/organize/naming'"
          ><span>命名模板<small>目录与文件如何命名</small></span
          ><span>↗</span></RouterLink
        ><RouterLink :to="{ path: '/settings/cloud', query: { section: 'classification' } }"
          ><span>归档分类<small>类型、地区与年份</small></span
          ><span>↗</span></RouterLink
        ><RouterLink :to="{ path: '/settings/cloud', query: { section: 'output' } }"
          ><span>STRM 输出<small>NAS 与 Emby 路径</small></span
          ><span>↗</span></RouterLink
        ><RouterLink to="/organize/quality"
          ><span>整理八维与洗版<small>维度排序、版本预览、共存策略</small></span
          ><span>↗</span></RouterLink
        ><RouterLink to="/organize/automation" class="text-link">设置自动整理 →</RouterLink>
      </div>
    </aside>
  </div>
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
