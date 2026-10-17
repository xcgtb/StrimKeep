<script setup lang="ts">
import { computed } from 'vue';
import type { Data } from '../api/domain';
import ConfirmAction from './ConfirmAction.vue';
import EvidenceDetails from './EvidenceDetails.vue';
const props = defineProps<{ changes: Data; busy: boolean; uncertain: boolean }>();
defineEmits<{ close: []; confirm: [] }>();
const keys = computed(() => Object.keys(props.changes));
</script>
<template>
  <ConfirmAction
    title="保存整理与播放设置"
    description="后续任务将使用本次保存的配置。启用洗版或源文件治理会影响真实视频；请核对修改后确认。"
    :danger="
      keys.some((k) =>
        [
          'quality8_mode',
          'official_delete_enabled',
          'governance_generated_enabled',
          'governance_source_mode',
        ].includes(k),
      )
    "
    :phrase="
      keys.includes('governance_source_mode')
        ? '确认修改治理来源'
        : keys.some((k) => ['official_delete_enabled', 'governance_generated_enabled'].includes(k))
          ? '启用源文件治理'
          : undefined
    "
    :busy="busy"
    :disabled="uncertain"
    @close="$emit('close')"
    @confirm="$emit('confirm')"
    ><EvidenceDetails :value="changes"
  /></ConfirmAction>
</template>
