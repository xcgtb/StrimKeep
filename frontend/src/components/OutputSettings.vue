<script setup lang="ts">
import { computed } from 'vue';
import { FileVideo, Save } from '@lucide/vue';
import { useCloudSettingsDraft } from '../composables/useCloudSettingsDraft';
import { cloudSections } from '../config/cloudFields';
import FormFields from './FormFields.vue';
import LocalPathPicker from './LocalPathPicker.vue';
import OperationState from './OperationState.vue';
import CloudSettingsConfirmation from './CloudSettingsConfirmation.vue';
const props = withDefaults(defineProps<{ kind?: 'personal' | 'share' }>(), { kind: 'personal' });
const { request, op, draft, confirmation, changed, changes, load, save } = useCloudSettingsDraft();
const outputKey = computed(() => (props.kind === 'share' ? 'share_output_root' : 'output_root'));
const embyKey = computed(() =>
  props.kind === 'share' ? 'share_emby_output_root' : 'emby_output_root',
);
const fields = computed(() =>
  cloudSections[0]!.fields
    .filter(
      (f) =>
        ![
          'output_root',
          'share_output_root',
          'emby_output_root',
          'share_emby_output_root',
        ].includes(f.key),
    )
    .map((f) => (f.key === 'public_url' ? { ...f, label: 'STRM 播放服务地址（302）' } : f)),
);
</script>
<template>
  <section class="panel strm-output-settings">
    <header class="section-heading">
      <div>
        <h2>
          <FileVideo :size="16" /> {{ kind === 'share' ? '分享库输出与播放' : '个人库输出与播放' }}
        </h2>
        <p>先保存输出与播放配置，再同步已确认的媒体资源。</p>
      </div>
      <button class="button small" :disabled="request.loading.value || op.busy.value" @click="load">
        读取已保存设置
      </button>
    </header>
    <OperationState
      :busy="request.loading.value || op.busy.value"
      :error="request.error.value || op.error.value"
      :uncertain="op.uncertain.value"
    />
    <template v-if="request.data.value"
      ><div class="form-grid">
        <LocalPathPicker
          v-model="draft[outputKey]"
          :label="kind === 'share' ? '分享库 NAS STRM 输出目录' : '个人库 NAS STRM 输出目录'"
          :disabled="op.busy.value || op.uncertain.value"
        /><label
          >Emby 可访问的输出路径<input
            v-model="draft[embyKey]"
            :disabled="op.busy.value || op.uncertain.value"
          /><small>填写 Emby 容器看到的同一目录路径。</small></label
        >
      </div>
      <h3 class="detail-section-title">生成时机与播放服务</h3>
      <p class="muted-text">以下生成策略和播放服务地址由两库共用；输出目录分别保存。</p>
      <FormFields
        v-model="draft"
        :fields="fields"
        :disabled="op.busy.value || op.uncertain.value"
      />
      <p class="rule-note mt18">
        整理归档完成 → 等待 {{ Number(draft.strm_delay_seconds || 0) }} 秒 → 生成 STRM → 请求 Emby
        扫描 → 等待实际入库确认。
      </p>
      <div class="actions mt18">
        <button
          class="button primary"
          :disabled="
            !changed.length || op.busy.value || op.uncertain.value || !!request.error.value
          "
          @click="confirmation = true"
        >
          <Save :size="14" />保存输出与联动设置</button
        ><RouterLink to="/settings/cloud?section=proxy" class="text-link"
          >Emby 反代设置 →</RouterLink
        >
      </div></template
    >
  </section>
  <CloudSettingsConfirmation
    v-if="confirmation"
    :changes="changes"
    :busy="op.busy.value"
    :uncertain="op.uncertain.value"
    @close="confirmation = false"
    @confirm="save"
  />
</template>
