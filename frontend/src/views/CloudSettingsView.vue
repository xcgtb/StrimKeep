<script setup lang="ts">
import { ref, computed, watch } from 'vue';
import { useCloudSettingsDraft } from '../composables/useCloudSettingsDraft';
import { records, strings, type Data } from '../api/domain';
import { useRoute } from 'vue-router';
import { cloudSections } from '../config/cloudFields';
import PageHeading from '../components/PageHeading.vue';
import FormFields from '../components/FormFields.vue';
import QualityRules from '../components/QualityRules.vue';
import NamingTemplates from '../components/NamingTemplates.vue';
import coverDefaults from '../config/quality-defaults.json';
import CloudSettingsConfirmation from '../components/CloudSettingsConfirmation.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
const route = useRoute();
const { request, op, draft, confirmation, preview, changed, changes, load, save, previewNaming } =
  useCloudSettingsDraft();
const tab = ref('output');
watch(
  () => [route.path, route.query.section],
  () => {
    const target =
      route.path === '/organize/naming'
        ? 'naming'
        : route.path === '/organize/quality'
          ? 'quality'
          : route.query.section;
    tab.value = cloudSections.some((s) => s.key === target) ? String(target) : 'output';
  },
  { immediate: true },
);
const visibleFields = computed(() =>
  tab.value === 'naming'
    ? section.value.fields.filter(
        (f) =>
          ![
            'movie_directory_template',
            'tv_directory_template',
            'season_directory_template',
            'movie_file_template',
            'tv_file_template',
          ].includes(f.key),
      )
    : section.value.fields,
);
function resetQuality() {
  const base = coverDefaults.cover.rules as Data[];
  draft.value = {
    ...draft.value,
    quality8_rules: [
      ...base.map((rule) => ({
        key: rule.key === 'dolby' ? 'dynamic_range' : rule.key,
        enabled: rule.enabled,
        tiers: structuredClone(rule.tiers),
        groups: structuredClone(rule.groups),
      })),
      { key: 'size', enabled: true, tiers: [], groups: [] },
    ],
  };
}

const section = computed(() => cloudSections.find((s) => s.key === tab.value)!);
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
</script>
<template>
  <PageHeading :title="section.title" subtitle="仅提交你修改的字段，其他配置保持原值。"
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
  <div class="module-settings">
    <div class="settings-content">
      <section v-if="request.data.value" class="panel settings-panel">
        <p class="muted-text">{{ section.description }}</p>
        <FormFields
          v-model="draft"
          :fields="visibleFields"
          :disabled="op.busy.value || op.uncertain.value"
        />
        <NamingTemplates
          v-if="tab === 'naming'"
          v-model="draft"
          :disabled="op.busy.value || op.uncertain.value"
        />
        <template v-if="tab === 'classification'"
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
        >
        <div v-if="tab === 'quality'" class="quality-section-heading">
          <p class="data-caption">
            整理八维读取 115 原视频大小。STRM 七维治理独立设置，不按文件大小比较。
          </p>
          <button
            type="button"
            class="button small"
            :disabled="op.busy.value || op.uncertain.value"
            @click="resetQuality"
          >
            恢复八维默认草稿
          </button>
        </div>
        <QualityRules
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
  <CloudSettingsConfirmation
    v-if="confirmation"
    :changes="changes"
    :busy="op.busy.value"
    :uncertain="op.uncertain.value"
    @close="confirmation = false"
    @confirm="save"
  />
</template>
