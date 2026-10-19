<script setup lang="ts">
import { computed, watch } from 'vue';
import { useCloudSettingsDraft } from '../composables/useCloudSettingsDraft';
import { records } from '../api/domain';
import QualityRules from './QualityRules.vue';
import CloudSettingsConfirmation from './CloudSettingsConfirmation.vue';
import OperationState from './OperationState.vue';
const emit = defineEmits<{ saved: [] }>();
const { request, op, draft, confirmation, changed, changes, load, save } = useCloudSettingsDraft();
const rules = computed({
  get: () => records(draft.value.share_quality_rules),
  set: (value) => {
    draft.value.share_quality_rules = value;
  },
});
async function commit() {
  await save();
  if (!confirmation.value && !op.error.value && !op.uncertain.value) emit('saved');
}
watch(
  () => draft.value.share_quality_enabled,
  (enabled) => {
    if (enabled === false) draft.value.share_quality_mode = 'preview';
  },
);
</script>
<template>
  <details class="module-config">
    <summary>分享库独立选版与洗版策略</summary>
    <section class="panel form-stack">
      <h2>分享 STRM 版本选择</h2>
      <p class="muted-text">
        按同一 TMDB 电影或同一集比较，先决定版本去留，再生成 STRM。仅处理当前账号分享库已登记的 NAS
        引用，115 文件保持不变。
      </p>
      <OperationState
        :busy="request.loading.value || op.busy.value"
        :error="request.error.value || op.error.value"
        :uncertain="op.uncertain.value"
      />
      <template v-if="request.data.value">
        <label class="checkbox-label"
          ><input
            v-model="draft.share_quality_enabled"
            type="checkbox"
          />启用分享库独立质量比较</label
        >
        <label
          >执行方式<select
            v-model="draft.share_quality_mode"
            aria-label="执行方式"
            :disabled="op.busy.value || op.uncertain.value"
          >
            <option value="preview">仅比较，已有版本保留</option>
            <option value="upgrade">预览确认后替换更优版本</option>
          </select></label
        >
        <label class="checkbox-label"
          ><input
            v-model="draft.share_quality_coexist_dv"
            type="checkbox"
          />杜比与非杜比版本共存（需不同输出名称）</label
        >
        <QualityRules v-model="rules" :disabled="op.busy.value || op.uncertain.value" />
        <p class="rule-note">
          平局或旧版更优时保留旧版。执行前再次核验来源、挂载、旧引用与配置；新引用核验后才清理旧引用及内容未变的自建附件。
        </p>
        <div class="action-row">
          <button
            class="button primary"
            :disabled="
              !changed.length ||
              request.loading.value ||
              !!request.error.value ||
              op.busy.value ||
              op.uncertain.value
            "
            @click="confirmation = true"
          >
            保存分享库策略</button
          ><button class="button" @click="load">重新读取</button>
        </div>
      </template>
    </section>
  </details>
  <CloudSettingsConfirmation
    v-if="confirmation"
    :changes="changes"
    :busy="op.busy.value"
    :uncertain="op.uncertain.value"
    @close="confirmation = false"
    @confirm="commit"
  />
</template>
