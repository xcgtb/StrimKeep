<script setup lang="ts">
import { computed } from 'vue';
import { useCloudSettingsDraft } from '../composables/useCloudSettingsDraft';
import { namingTemplateFields, validateTemplate } from '../config/namingVariables';
import PageHeading from '../components/PageHeading.vue';
import WorkflowRail from '../components/WorkflowRail.vue';
import NamingTemplates from '../components/NamingTemplates.vue';
import FormFields from '../components/FormFields.vue';
import OperationState from '../components/OperationState.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import CloudSettingsConfirmation from '../components/CloudSettingsConfirmation.vue';
const { request, op, draft, confirmation, preview, changed, changes, load, save, previewNaming } =
  useCloudSettingsDraft();
const invalid = computed(() =>
  namingTemplateFields.some((f) => !!validateTemplate(f.key, String(draft.value[f.key] || ''))),
);
</script>
<template>
  <PageHeading
    title="自定义命名模板"
    subtitle="电影、剧集、季目录分别管理，用中文变量词典组成你的命名方式。"
    eyebrow="媒体加工"
    ><button class="button" :disabled="op.busy.value" @click="load">重新读取</button
    ><button
      class="button primary"
      :disabled="
        !changed.length || invalid || op.busy.value || op.uncertain.value || !!request.error.value
      "
      @click="confirmation = true"
    >
      保存 {{ changed.length }} 项修改
    </button></PageHeading
  >
  <WorkflowRail :active="1" /><OperationState
    :busy="request.loading.value || op.busy.value"
    :error="request.error.value || op.error.value"
    :uncertain="op.uncertain.value"
  />
  <template v-if="request.data.value"
    ><section class="panel naming-mode-panel">
      <FormFields
        v-model="draft"
        :fields="[
          {
            key: 'naming_style',
            label: '命名方式',
            type: 'select',
            options: [
              { value: 'standard', label: '标准命名' },
              { value: 'compact', label: '简洁命名' },
              { value: 'original', label: '保留原名' },
              { value: 'template', label: '自定义模板' },
            ],
          },
        ]"
        :disabled="op.busy.value || op.uncertain.value"
      />
      <p class="data-caption">选择「自定义模板」后，后续整理任务会使用下方模板。</p>
    </section>
    <NamingTemplates v-model="draft" :disabled="op.busy.value || op.uncertain.value" />
    <section class="panel">
      <header class="section-heading">
        <div>
          <h2>后端命名预览</h2>
          <p>用当前草稿核验识别与命名结果，预览不会保存配置。</p>
        </div>
      </header>
      <div class="action-row">
        <button
          class="button primary"
          :disabled="invalid || op.busy.value || op.uncertain.value"
          @click="previewNaming()"
        >
          运行后端命名预览</button
        ><button
          class="button"
          :disabled="op.busy.value || op.uncertain.value"
          @click="previewNaming(true)"
        >
          查看后端字段说明
        </button>
      </div>
      <EvidenceDetails v-if="preview" :value="preview" /><RouterLink
        to="/organize/workspace"
        class="text-link"
        >返回影视整理 →</RouterLink
      >
    </section>
  </template>
  <CloudSettingsConfirmation
    v-if="confirmation"
    :changes="changes"
    :busy="op.busy.value"
    :uncertain="op.uncertain.value"
    @close="confirmation = false"
    @confirm="save"
  />
</template>
