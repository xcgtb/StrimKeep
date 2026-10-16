<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue';
import { readData, writeData, object, records, strings, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useRoute } from 'vue-router';
import { cloudSections, namingKeys } from '../config/cloudFields';
import PageHeading from '../components/PageHeading.vue';
import FormFields from '../components/FormFields.vue';
import QualityRules from '../components/QualityRules.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
const route = useRoute();
const request = useRequest<Data>(),
  op = useOperation(),
  draft = ref<Data>({}),
  tab = ref('output'),
  confirmation = ref(false),
  preview = ref<Data>();
watch(
  () => route.query.section,
  (v) => {
    if (cloudSections.some((s) => s.key === v)) tab.value = String(v);
  },
  { immediate: true },
);
const section = computed(() => cloudSections.find((s) => s.key === tab.value)!);
const changed = computed(() =>
  Object.keys(draft.value).filter(
    (k) =>
      JSON.stringify(draft.value[k]) !== JSON.stringify(object(request.data.value?.settings)[k]),
  ),
);
async function load() {
  const r = await request.run((signal) => readData('/api/cloud/settings', signal));
  if (r) {
    draft.value = structuredClone(object(r.settings));
    op.reconcile();
  }
}
async function save() {
  const r = await op.run(() =>
    writeData(
      '/api/cloud/settings',
      Object.fromEntries(changed.value.map((k) => [k, draft.value[k]])),
    ),
  );
  if (r) {
    confirmation.value = false;
    request.data.value = r;
    draft.value = structuredClone(object(r.settings));
  }
}
async function previewNaming(details = false) {
  const r = await op.run(() =>
    writeData(details ? '/api/cloud/naming/details' : '/api/cloud/naming/preview', {
      settings: Object.fromEntries(
        namingKeys.filter((k) => k in draft.value).map((k) => [k, draft.value[k]]),
      ),
    }),
  );
  if (r) preview.value = r;
}
function region(i: number, key: string, value: unknown) {
  draft.value = {
    ...draft.value,
    region_groups: records(draft.value.region_groups).map((r, j) =>
      j === i ? { ...r, [key]: value } : r,
    ),
  };
}
function addRegion() {
  draft.value = {
    ...draft.value,
    region_groups: [...records(draft.value.region_groups), { name: '新地区组', countries: [] }],
  };
}
function removeRegion(i: number) {
  draft.value = {
    ...draft.value,
    region_groups: records(draft.value.region_groups).filter((_, j) => i !== j),
  };
}
onMounted(load);
</script>
<template>
  <PageHeading
    title="整理与播放设置"
    subtitle="统一设置识别命名、分类、过滤、洗版、STRM 输出和通知。"
    ><button
      class="button primary"
      :disabled="!changed.length || op.busy.value || op.uncertain.value || !!request.error.value"
      @click="confirmation = true"
    >
      保存 {{ changed.length }} 项修改
    </button></PageHeading
  ><OperationState
    :busy="request.loading.value || op.busy.value"
    :error="request.error.value || op.error.value"
    :uncertain="op.uncertain.value"
  />
  <div class="settings-layout">
    <nav class="settings-index">
      <p class="settings-nav-caption">设置分区</p>
      <button
        v-for="s in cloudSections"
        :key="s.key"
        :class="{ active: tab === s.key }"
        @click="tab = s.key"
      >
        {{ s.title }}
      </button>
    </nav>
    <div class="settings-content">
      <select v-model="tab" class="section-select" aria-label="设置分区">
        <option v-for="s in cloudSections" :key="s.key" :value="s.key">{{ s.title }}</option>
      </select>
      <section v-if="request.data.value" class="panel settings-panel">
        <h2>{{ section.title }}</h2>
        <p class="muted-text">{{ section.description }}</p>
        <FormFields
          v-model="draft"
          :fields="section.fields"
          :disabled="op.busy.value || op.uncertain.value"
        /><template v-if="tab === 'classification'"
          ><h3>地区归组</h3>
          <div v-for="(r, i) in records(draft.region_groups)" :key="i" class="repeat-row">
            <label
              >组名称<input
                :value="r.name"
                @input="region(i, 'name', ($event.target as HTMLInputElement).value)" /></label
            ><label
              >国家/地区代码（逗号分隔）<input
                :value="strings(r.countries).join(',')"
                @change="
                  region(
                    i,
                    'countries',
                    ($event.target as HTMLInputElement).value
                      .split(',')
                      .map((s) => s.trim())
                      .filter(Boolean),
                  )
                " /></label
            ><button class="button small danger-text" @click="removeRegion(i)">移除</button>
          </div>
          <button class="button small" @click="addRegion">添加地区组</button></template
        ><QualityRules
          v-if="tab === 'quality'"
          :model-value="records(draft.quality8_rules)"
          :disabled="op.busy.value || op.uncertain.value"
          @update:model-value="(v) => (draft.quality8_rules = v)"
        />
        <div class="action-row">
          <button class="button" @click="load">重新读取已保存设置</button
          ><button
            v-if="tab === 'naming'"
            class="button"
            :disabled="op.busy.value || op.uncertain.value"
            @click="previewNaming()"
          >
            预览模板效果</button
          ><button
            v-if="tab === 'naming'"
            class="button"
            :disabled="op.busy.value || op.uncertain.value"
            @click="previewNaming(true)"
          >
            查看模板字段说明
          </button>
        </div>
      </section>
      <section v-if="preview" class="panel">
        <h2>模板预览</h2>
        <EvidenceDetails :value="preview" />
      </section>
    </div>
  </div>
  <ConfirmAction
    v-if="confirmation"
    title="保存整理与播放设置"
    description="后续手动与自动任务将使用这些设置。启用洗版或官方源删除会影响真实视频；变更反代端口会重启相关监听。请核对本次修改。"
    :danger="
      changed.some((k) =>
        ['quality8_mode', 'official_delete_enabled', 'governance_generated_enabled'].includes(k),
      )
    "
    :phrase="
      changed.some((k) => ['official_delete_enabled', 'governance_generated_enabled'].includes(k))
        ? '启用源文件治理'
        : undefined
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirmation = false"
    @confirm="save"
    ><EvidenceDetails :value="Object.fromEntries(changed.map((k) => [k, draft[k]]))"
  /></ConfirmAction>
</template>
