<script setup lang="ts">
import { computed, ref } from 'vue';
import { cloudSections } from '../config/cloudFields';
import { namingTemplateFields, validateTemplate } from '../config/namingVariables';
import { useCloudSettingsDraft } from '../composables/useCloudSettingsDraft';
import ModalDialog from './ModalDialog.vue';
import FormFields from './FormFields.vue';
import NamingTemplates from './NamingTemplates.vue';
import OperationState from './OperationState.vue';
import EvidenceDetails from './EvidenceDetails.vue';
import CloudSettingsConfirmation from './CloudSettingsConfirmation.vue';
const props = defineProps<{ section: 'naming' | 'classification' }>();
const emit = defineEmits<{ close: []; saved: [] }>();
const { request, op, draft, confirmation, preview, changed, changes, save, previewNaming } =
  useCloudSettingsDraft();
const discard = ref(false);
const section = computed(() => cloudSections.find((s) => s.key === props.section)!);
const fields = computed(() =>
  section.value.fields.filter((f) => !namingTemplateFields.some((t) => t.key === f.key)),
);
const invalid = computed(
  () =>
    props.section === 'naming' &&
    namingTemplateFields.some((f) => !!validateTemplate(f.key, String(draft.value[f.key] || ''))),
);
function close() {
  if (op.busy.value) return;
  if (changed.value.length) discard.value = true;
  else emit('close');
}
async function commit() {
  await save();
  if (!op.error.value && !op.uncertain.value) emit('saved');
}
</script>
<template>
  <ModalDialog
    :title="section === undefined ? '设置' : props.section === 'naming' ? '命名模板' : '分类规则'"
    @close="close"
  >
    <p class="notice-bar">
      这是全局共享设置，保存后影响后续预览；当前分享与文件选择会保留，执行计划需要重新预览。
    </p>
    <OperationState
      :busy="request.loading.value || op.busy.value"
      :error="request.error.value || op.error.value"
      :uncertain="op.uncertain.value"
    />
    <template v-if="request.data.value">
      <FormFields
        v-model="draft"
        :fields="fields"
        :disabled="op.busy.value || op.uncertain.value"
      />
      <NamingTemplates
        v-if="props.section === 'naming'"
        v-model="draft"
        :disabled="op.busy.value || op.uncertain.value"
      />
      <div class="action-row">
        <button class="button" :disabled="invalid || op.busy.value" @click="previewNaming()">
          预览命名效果
        </button>
        <button
          class="button primary"
          :disabled="!changed.length || invalid || op.busy.value || op.uncertain.value"
          @click="confirmation = true"
        >
          保存 {{ changed.length }} 项修改
        </button>
        <button class="button" @click="close">返回当前分享</button>
      </div>
      <EvidenceDetails v-if="preview" :value="preview" />
    </template>
    <div v-if="discard" class="notice-bar" role="alert">
      有未保存修改。<button class="button" @click="discard = false">继续编辑</button
      ><button class="button" @click="emit('close')">放弃修改并返回</button>
    </div>
    <CloudSettingsConfirmation
      v-if="confirmation"
      :changes="changes"
      :busy="op.busy.value"
      :uncertain="op.uncertain.value"
      @close="confirmation = false"
      @confirm="commit"
    />
  </ModalDialog>
</template>
