<script setup lang="ts">
import { computed, ref } from 'vue';
import { FileVideo, Save, Clock3, FolderOpen, Link2, Radio, CalendarClock } from '@lucide/vue';
import { useCloudSettingsDraft } from '../composables/useCloudSettingsDraft';
import { cloudSections } from '../config/cloudFields';
import FormFields from './FormFields.vue';
import LocalPathPicker from './LocalPathPicker.vue';
import OperationState from './OperationState.vue';
import CloudSettingsConfirmation from './CloudSettingsConfirmation.vue';
import EmbyMappingStatus from './EmbyMappingStatus.vue';
import { date } from '../composables/format';
import { object } from '../api/domain';
import { useVisiblePoll } from '../composables/useVisiblePoll';
const emit = defineEmits<{ saved: [] }>();
const props = withDefaults(defineProps<{ kind?: 'personal' | 'share' }>(), { kind: 'personal' });
const { request, op, draft, confirmation, changed, changes, load, save } = useCloudSettingsDraft();
const outputKey = computed(() => (props.kind === 'share' ? 'share_output_root' : 'output_root'));
const embyKey = computed(() =>
  props.kind === 'share' ? 'share_emby_output_root' : 'emby_output_root',
);
const busy = computed(() => op.busy.value || op.uncertain.value);
const unit = ref(1);
const delay = computed({
  get: () => Number(draft.value.strm_delay_seconds || 0) / unit.value,
  set: (n: number) => {
    draft.value.strm_delay_seconds = Math.round(n * unit.value * 1000) / 1000;
  },
});
const generationFields = computed(() =>
  cloudSections[0]!.fields.filter((f) =>
    [
      'generate_strm_enabled',
      'nfo_enabled',
      'artwork_enabled',
      'operation_delay_seconds',
      ...(unit.value === 1 ? ['strm_delay_seconds'] : []),
    ].includes(f.key),
  ),
);
const playbackFields = computed(() =>
  cloudSections[0]!.fields
    .filter((f) => ['public_url', 'external_url'].includes(f.key))
    .map((f) =>
      f.key === 'public_url'
        ? {
            ...f,
            label: 'STRM 播放服务地址（302）',
            hint: '写入 STRM 的地址，须能由 Emby 与播放器访问；支持内网地址或反代子路径。',
          }
        : f,
    ),
);
const scheduleFields = computed(() =>
  cloudSections[0]!.fields.filter((f) =>
    [
      'strm_scan_cron',
      'strm_scan_timezone',
      'strm_scan_start_mode',
      'strm_reconcile_enabled',
    ].includes(f.key),
  ),
);
const protectionFields = computed(() =>
  cloudSections[0]!.fields.filter((f) => f.key === 'strm_block_feiniu_ua'),
);
const notificationFields = computed(() =>
  cloudSections
    .find((s) => s.key === 'notifications')!
    .fields.filter((f) =>
      [
        'emby_refresh_enabled',
        'ingest_retry_seconds',
        'ingest_max_attempts',
        'notify_enabled',
        'notify_strm',
      ].includes(f.key),
    ),
);
async function saveAndNotify() {
  await save();
  if (!op.error.value && !op.uncertain.value && !confirmation.value) emit('saved');
}
useVisiblePoll(() => {
  if (!changed.value.length && !confirmation.value && !busy.value) return load();
}, 20000);
</script>
<template>
  <div class="strm-output-settings workspace-card-stack">
    <header class="section-heading">
      <div>
        <h2>
          <FileVideo :size="18" /> {{ kind === 'share' ? '分享库输出与播放' : '个人库输出与播放' }}
        </h2>
        <p>配置、核验与执行分别确认，修改后在本页保存。</p>
      </div>
      <button class="button small" :disabled="request.loading.value || busy" @click="load">
        撤销本页修改
      </button>
    </header>
    <OperationState
      :busy="request.loading.value || op.busy.value"
      :error="request.error.value || op.error.value"
      :uncertain="op.uncertain.value"
    />
    <template v-if="request.data.value">
      <section class="panel workflow-config-card">
        <header class="section-heading">
          <h3><Clock3 :size="17" />生成时机</h3>
          <span class="module-tag">两库共用</span>
        </header>
        <p class="muted-text">归档完成后生成播放引用；操作间隔用于控制 115 请求频率。</p>
        <div class="delay-unit-row">
          <span>延迟显示单位</span>
          <div class="segmented">
            <button
              v-for="(label, value) in { 1: '秒', 60: '分钟', 3600: '小时' }"
              :key="value"
              type="button"
              :class="{ active: unit === Number(value) }"
              @click="unit = Number(value)"
            >
              {{ label }}
            </button>
          </div>
        </div>
        <FormFields v-model="draft" :fields="generationFields" :disabled="busy" /><label
          v-if="unit !== 1"
          >STRM 延迟生成（{{ unit === 60 ? '分钟' : '小时' }}）<input
            v-model.number="delay"
            type="number"
            min="0"
            :max="86400 / unit"
            step="any"
            :disabled="busy"
        /></label>
        <p class="rule-note">
          归档 → 等待 {{ Number(draft.strm_delay_seconds || 0) }} 秒 → 生成 STRM → Emby 核验
        </p>
      </section>
      <section v-if="kind === 'personal'" class="panel workflow-config-card">
        <header class="section-heading">
          <h3><CalendarClock :size="17" />定时全量同步</h3>
          <span class="module-tag">各账号独立来源</span>
        </header>
        <p class="muted-text">
          使用各账号在本页保存的 STRM 同步来源，只更新播放引用，不移动或重命名网盘文件。
        </p>
        <FormFields v-model="draft" :fields="scheduleFields" :disabled="busy" />
        <p class="rule-note">同一目录已有同步任务时跳过本轮，避免重复排队。</p>
        <p class="muted-text">
          上次投递：{{ date(Number(object(request.data.value.schedule).last_run))
          }}<br />预计下次：{{
            object(request.data.value.schedule).enabled
              ? date(Number(object(request.data.value.schedule).next_run))
              : '未启用'
          }}（按已保存配置；队列顺序影响实际开始时间）
        </p>
        <p class="rule-note">
          失效引用清理：连续两次完整扫描均缺失，且挂载身份、文件内容核验通过后，才删除 NAS 自建 STRM
          及未变化的自建附件；不会删除 115 源文件。关闭后仅补生成/更新。
        </p>
      </section>
      <section class="panel workflow-config-card">
        <header class="section-heading">
          <h3><FolderOpen :size="17" />输出与路径映射</h3>
          <span class="module-tag">{{ kind === 'share' ? '分享库' : '个人库' }}</span>
        </header>
        <LocalPathPicker
          v-model="draft[outputKey]"
          :label="kind === 'share' ? '分享库 NAS STRM 输出目录' : '个人库 NAS STRM 输出目录'"
          :disabled="busy"
        /><label
          >Emby 反代识别路径<input v-model="draft[embyKey]" :disabled="busy" /><small
            >用于反代定位播放引用。实际入库路径由每个 Emby 连接单独配置，见下方映射核对。</small
          ></label
        ><RouterLink to="/tasks/ingestion" class="text-link"
          >配置 Emby 连接与查看入库回执 →</RouterLink
        >
      </section>
      <EmbyMappingStatus :kind="kind" />
      <section class="panel workflow-config-card">
        <header class="section-heading">
          <h3><Link2 :size="17" />播放地址</h3>
          <span class="module-tag">302 / 代理</span>
        </header>
        <FormFields v-model="draft" :fields="playbackFields" :disabled="busy" />
        <FormFields v-model="draft" :fields="protectionFields" :disabled="busy" />
        <p class="muted-text">
          外网地址用于复制播放链接；已生成的 STRM 通过同步更新。需要 Cookie 的 115
          资源自动使用受控代理。
        </p>
        <RouterLink to="/settings/cloud?section=proxy" class="text-link"
          >Emby 反代监听设置 →</RouterLink
        >
      </section>
      <section class="panel workflow-config-card">
        <header class="section-heading">
          <h3><Radio :size="17" />入库核验与通知</h3>
        </header>
        <FormFields v-model="draft" :fields="notificationFields" :disabled="busy" />
        <p class="muted-text">重试仅核验 Emby 入库，不会重复转存、移动或删除源文件。</p>
      </section>
      <div class="module-save-bar">
        <span class="muted-text">{{
          changed.length ? `${changed.length} 项修改待保存` : '当前配置已保存'
        }}</span
        ><button
          class="button primary"
          :disabled="!changed.length || busy || !!request.error.value"
          @click="confirmation = true"
        >
          <Save :size="15" />保存输出与联动设置
        </button>
      </div>
    </template>
  </div>
  <CloudSettingsConfirmation
    v-if="confirmation"
    :changes="changes"
    :busy="op.busy.value"
    :uncertain="op.uncertain.value"
    @close="confirmation = false"
    @confirm="saveAndNotify"
  />
</template>
