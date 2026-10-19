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
    description="后续任务将使用本次保存的配置，请核对本次变更及其影响后确认。"
    :danger="
      keys.some((k) =>
        ['quality8_mode', 'official_delete_enabled', 'governance_generated_enabled'].includes(k),
      )
    "
    :phrase="
      keys.some((k) => ['official_delete_enabled', 'governance_generated_enabled'].includes(k))
        ? '启用源文件治理'
        : undefined
    "
    :busy="busy"
    :disabled="uncertain"
    @close="$emit('close')"
    @confirm="$emit('confirm')"
    ><p
      v-if="
        keys.some((k) =>
          ['strm_scan_cron', 'strm_scan_start_mode', 'strm_reconcile_enabled'].includes(k),
        )
      "
      class="rule-note"
    >
      定时同步将扫描所有账号已保存的 STRM
      来源。启用失效引用清理后，连续两次完整扫描缺失且挂载与内容核验通过的 NAS 自建 STRM
      及附件会被删除；115 源文件不受影响。重新启用或调整排期按所选启动方式执行，旧的执行记录保留。
    </p>
    <p v-if="keys.some((k) => k.startsWith('share_quality_'))" class="rule-note">
      分享库策略独立生效。执行洗版需重新预览并确认具体版本，只替换当前账号分享库已登记的 NAS
      引用；平局、内容变化或挂载变化时保留现有文件。
    </p>
    <EvidenceDetails :value="changes"
  /></ConfirmAction>
</template>
