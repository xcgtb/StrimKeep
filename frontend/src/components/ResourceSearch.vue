<script setup lang="ts">
import { ref, watch, onMounted } from 'vue';
import { request } from '../api/client';
import { readData, writeData, records, object, uuid, type Data } from '../api/domain';
import type { MediaCard } from '../types/api';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { useUi } from '../stores/ui';
import FormFields, { type Field } from './FormFields.vue';
import ConfirmAction from './ConfirmAction.vue';
import EvidenceDetails from './EvidenceDetails.vue';
import OperationState from './OperationState.vue';
const props = defineProps<{ card?: MediaCard }>(),
  ui = useUi(),
  op = useOperation(),
  candidates = useRequest<Data>(),
  providers = useRequest<Data>(),
  directories = useRequest<Data>(),
  jobs = useRequest<Data>(),
  config = useRequest<Data>(),
  form = ref<Data>({
    title: '',
    media_type: 'movie',
    tmdb_id: '',
    year: '',
    season: '',
    episode: '',
  }),
  provider = ref(''),
  confirmation = ref(''),
  selected = ref<Data>(),
  operationId = ref(''),
  link = ref('');
const fields: Field[] = [
  { key: 'title', label: '作品名称' },
  {
    key: 'media_type',
    label: '媒体类型',
    type: 'select',
    options: [
      { value: 'movie', label: '电影' },
      { value: 'tv', label: '剧集' },
    ],
  },
  { key: 'tmdb_id', label: 'TMDB 编号（订阅必填）' },
  { key: 'year', label: '年份（可留空）' },
  { key: 'season', label: '季号（仅剧集，可留空）' },
  { key: 'episode', label: '集号（仅剧集，可留空）' },
];
watch(
  () => props.card,
  (card) => {
    form.value = {
      title: card?.title || card?.name || '',
      media_type: card?.type || 'movie',
      tmdb_id: card?.tmdb_id || '',
      year: card?.year || '',
      season: '',
      episode: '',
    };
  },
  { immediate: true },
);
watch(
  form,
  () => {
    candidates.cancel();
    candidates.data.value = undefined;
    selected.value = undefined;
    confirmation.value = '';
  },
  { deep: true },
);
async function search() {
  selected.value = undefined;
  link.value = '';
  const body = Object.fromEntries(
    Object.entries(form.value).filter(
      ([k, v]) =>
        v !== '' && (form.value.media_type === 'tv' || !['season', 'episode'].includes(k)),
    ),
  );
  await candidates.run((signal) =>
    request<Data>('/api/upstream/search', { method: 'POST', signal, body, timeoutMs: 60000 }),
  );
  await Promise.allSettled([
    providers.run((signal) => readData('/api/upstream/providers', signal)),
    directories.run((signal) => readData('/api/upstream/directories', signal)),
  ]);
}
function loadJobs() {
  return jobs.run((signal) => readData('/api/upstream/jobs', signal));
}
async function verifyJobs() {
  const result = await loadJobs();
  if (result && records(result.items).some((j) => j.id === operationId.value)) op.reconcile(true);
}
async function execute() {
  const action = confirmation.value;
  if (action === 'acknowledge') op.reconcile(true);
  operationId.value = uuid();
  const body: Data = { operation_id: operationId.value, provider: provider.value, confirmed: true };
  let endpoint = '/api/upstream/transfer';
  if (action === 'resource_subscribe') {
    endpoint = '/api/upstream/operations/resource_subscribe';
    Object.assign(body, {
      title: form.value.title,
      media_type: form.value.media_type,
      tmdb_id: form.value.tmdb_id,
      notify_import: true,
    });
  } else if (action === 'acknowledge') {
    endpoint = `/api/upstream/jobs/${selected.value?.id}/acknowledge`;
  } else {
    body.handle = selected.value?.handle;
    if (action === 'copy_link') endpoint = '/api/upstream/operations/copy_link';
  }
  const r = await op.run(() => writeData(endpoint, body));
  if (r) {
    confirmation.value = '';
    await loadJobs();
  }
}
async function copy(row: Data) {
  const r = await op.run(() => readData(`/api/upstream/jobs/${row.id}/link`));
  if (r) {
    link.value = String(r.link || '');
    try {
      await navigator.clipboard.writeText(link.value);
      ui.notify('分享链接已复制');
    } catch {
      ui.notify('请从下方结果手动复制链接。');
    }
  }
}
onMounted(() => {
  void config.run((signal) => readData('/api/upstream/config', signal));
  void loadJobs();
});
useVisiblePoll(
  () =>
    records(jobs.data.value?.items).some((j) => j.state === 'submitting') ? loadJobs() : undefined,
  4000,
);
</script>
<template>
  <div class="resource-search">
    <OperationState
      :busy="op.busy.value || candidates.loading.value"
      :error="op.error.value || candidates.error.value"
      :uncertain="op.uncertain.value"
    />
    <p v-if="!object(config.data.value?.settings).enabled" class="notice-bar">
      外部资源接入尚未启用，请先在外部服务设置中配置。
    </p>
    <form @submit.prevent="search">
      <FormFields v-model="form" :fields="fields" :disabled="op.busy.value || op.uncertain.value" />
      <div class="action-row">
        <button
          class="button primary"
          :disabled="!form.title || candidates.loading.value || op.busy.value || op.uncertain.value"
        >
          搜索候选资源</button
        ><button
          class="button"
          type="button"
          :disabled="
            !provider || !form.tmdb_id || !form.title || op.busy.value || op.uncertain.value
          "
          @click="confirmation = 'resource_subscribe'"
        >
          订阅此作品的资源
        </button>
      </div>
    </form>
    <section v-if="providers.data.value || providers.error.value" class="target-panel">
      <label
        >上游保存目标<select v-if="records(providers.data.value?.items).length" v-model="provider">
          <option value="">请选择目标</option>
          <option
            v-for="p in records(providers.data.value?.items)"
            :key="String(p.id)"
            :value="p.id"
          >
            {{ p.name }}
          </option></select
        ><input v-else v-model="provider" placeholder="填写上游明确提供的目标标识"
      /></label>
      <p v-if="providers.error.value" class="inline-error">{{ providers.error.value }}</p>
      <p v-if="directories.error.value" class="inline-error">{{ directories.error.value }}</p>
      <div
        v-for="d in records(directories.data.value?.items).filter((d) => d.id === provider)"
        :key="String(d.id)"
      >
        <p>上游保存位置：{{ d.directory_path || d.directory_id || '未提供' }}</p>
      </div>
      <p class="muted-text">
        当前积分上限：{{
          object(config.data.value?.settings).max_points ?? '尚未读取'
        }}；具体费用以候选及上游回执为准。
      </p>
    </section>
    <div class="candidate-cards">
      <article
        v-for="(c, i) in records(candidates.data.value?.items)"
        :key="String(c.handle || i)"
        class="panel candidate-card"
      >
        <header>
          <h3>{{ c.title }}</h3>
          <span class="small-status">{{ c.verification || '校验状态未知' }}</span>
        </header>
        <div class="resource-specs">
          <span>{{ c.provider || c.source || '来源未知' }}</span
          ><span v-if="c.resolution || c.quality">{{ c.resolution || c.quality }}</span
          ><span v-if="c.video_effect">{{ c.video_effect }}</span
          ><span v-if="c.codec">{{ c.codec }}</span
          ><span v-if="c.size">{{ c.size }}</span>
        </div>
        <p>
          分享者 {{ c.publisher || '未提供' }} · 积分 {{ c.points || '未提供' }} ·
          {{ c.unlocked === true ? '已解锁' : c.unlocked === false ? '未解锁' : '解锁状态未知' }}
        </p>
        <p v-if="c.note" class="muted-text">{{ c.note }}</p>
        <details>
          <summary>完整规格与来源</summary>
          <EvidenceDetails :value="c" />
        </details>
        <div class="action-row">
          <button
            class="button primary"
            :disabled="!c.transferable || !provider || op.busy.value || op.uncertain.value"
            @click="
              selected = c;
              confirmation = 'transfer';
            "
          >
            转存到所选目标</button
          ><button
            class="button"
            :disabled="!c.handle || !provider || op.busy.value || op.uncertain.value"
            @click="
              selected = c;
              confirmation = 'copy_link';
            "
          >
            获取分享链接
          </button>
        </div>
      </article>
    </div>
    <p
      v-if="candidates.data.value && !records(candidates.data.value.items).length"
      class="muted-text"
    >
      没有找到候选资源。
    </p>
    <p v-if="link" class="path-box">{{ link }}</p>
    <details class="operation-result" :open="!!operationId">
      <summary>外部操作回执与重复保护</summary>
      <button class="button small" @click="verifyJobs">读取原操作核实结果</button>
      <p v-if="jobs.error.value" class="inline-error">{{ jobs.error.value }}</p>
      <article
        v-for="j in records(jobs.data.value?.items)"
        :key="String(j.id)"
        class="automation-rule"
      >
        <div>
          <strong>{{ j.title }}</strong>
          <p>{{ j.state }} · {{ j.message }}</p>
        </div>
        <button class="button small" @click="copy(j)">读取已取得链接</button
        ><button
          class="button small danger-text"
          @click="
            selected = j;
            confirmation = 'acknowledge';
          "
        >
          已人工核实，清除保护记录
        </button>
      </article>
    </details>
    <ConfirmAction
      v-if="confirmation"
      :title="confirmation === 'acknowledge' ? '清除重复保护记录' : '确认外部资源操作'"
      :description="
        confirmation === 'acknowledge'
          ? '先在上游核实任务结果。清除后同一资源可以再次提交，可能造成重复转存或重复扣费。'
          : confirmation === 'resource_subscribe'
            ? '向当前配置的上游添加资源获取订阅，目标为所选网盘；这与本地追更通知订阅不同。'
            : '使用当前所选候选与目标执行上游操作，可能按上游规则消耗积分。请求接受不等于转存、整理或入库完成。'
      "
      :phrase="confirmation === 'acknowledge' ? '已核实上游结果' : undefined"
      :busy="op.busy.value"
      :disabled="op.uncertain.value"
      @close="confirmation = ''"
      @confirm="execute"
      ><EvidenceDetails :value="confirmation === 'resource_subscribe' ? form : selected"
    /></ConfirmAction>
  </div>
</template>
