<script setup lang="ts">
import { computed } from 'vue';
import { RouterLink } from 'vue-router';
import {
  ArrowDownUp,
  Bell,
  CalendarClock,
  FolderOpen,
  SlidersHorizontal,
  Sparkles,
  WandSparkles,
} from '@lucide/vue';
import PageHeading from '../components/PageHeading.vue';
import AccountPicker from '../components/AccountPicker.vue';
import SectionCard from '../components/SectionCard.vue';
import FormFields from '../components/FormFields.vue';
import OperationState from '../components/OperationState.vue';
import CloudSettingsConfirmation from '../components/CloudSettingsConfirmation.vue';
import { useCloudSettingsDraft } from '../composables/useCloudSettingsDraft';
import { cloudSections } from '../config/cloudFields';
import { useCloud } from '../stores/cloud';

const cloud = useCloud();
const { op, draft, changed, changes, load, save, confirmation } = useCloudSettingsDraft();

/** 配置类区块（原先散落在 /settings/cloud?section=*）按整理流程排序 */
const ORDER: { key: string; icon: unknown; required?: boolean }[] = [
  { key: 'filters', icon: SlidersHorizontal },
  { key: 'classification', icon: FolderOpen },
  { key: 'quality', icon: SlidersHorizontal },
  { key: 'notifications', icon: Bell },
];
const sections = computed(() =>
  ORDER.map((entry) => ({
    ...entry,
    section: cloudSections.find((s) => s.key === entry.key),
  })).filter((item) => item.section),
);
const dirty = computed(() => changed.value.length > 0);

/** 操作类页面（跑起来的地方），配置好之后从这里进 */
const actions = [
  {
    to: '/organize/workspace',
    title: '扫描与整理',
    desc: '扫描来源目录，识别后按下面的规则命名、归档、择优。',
    icon: ArrowDownUp,
  },
  {
    to: '/organize/automation',
    title: '自动整理',
    desc: '配置 Cron 定时与独立整理规则，让它自己跑。',
    icon: CalendarClock,
  },
  {
    to: '/organize/quality',
    title: '八维与洗版',
    desc: '逐维排序与版本预览；洗版是否执行单独确认。',
    icon: Sparkles,
  },
  {
    to: '/organize/naming',
    title: '命名模板',
    desc: '目录与文件命名模板，含 29 个变量与实时预览。',
    icon: WandSparkles,
  },
];
</script>

<template>
  <PageHeading
    title="影视整理"
    subtitle="一套配置管一条链：从哪里扫 → 什么算有效文件 → 怎么命名 → 归到哪 → 怎么择优 → 删什么 → 何时自动跑 → 跑完通知谁。"
  />
  <AccountPicker />
  <OperationState
    :busy="op.busy.value"
    :error="op.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />

  <SectionCard
    title="开始整理"
    desc="规则配好之后，从这几处执行。扫描只在预览里给结论，确认后才动文件。"
  >
    <template #icon><ArrowDownUp :size="17" /></template>
    <div class="action-cards">
      <RouterLink v-for="a in actions" :key="a.to" :to="a.to" class="action-card">
        <component :is="a.icon" :size="16" />
        <span class="action-text">
          <strong>{{ a.title }}</strong>
          <small>{{ a.desc }}</small>
        </span>
      </RouterLink>
    </div>
  </SectionCard>

  <div class="section-stack">
    <SectionCard
      v-for="item in sections"
      :key="item.key"
      :title="item.section!.title"
      :desc="item.section!.description"
    >
      <template #icon><component :is="item.icon" :size="17" /></template>
      <FormFields
        v-model="draft"
        :fields="item.section!.fields"
        :disabled="op.busy.value || op.uncertain.value"
      />
    </SectionCard>
  </div>

  <div class="action-row save-row">
    <span v-if="dirty" class="dirty-hint">{{ changed.length }} 项已修改，未保存</span>
    <span v-else class="muted-text">当前配置与已保存的一致</span>
    <button class="button" :disabled="!cloud.id" @click="load">重新读取</button>
    <button
      class="button primary"
      :disabled="!dirty || op.busy.value || op.uncertain.value"
      @click="confirmation = true"
    >
      核对并保存
    </button>
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

<style scoped>
.section-stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-top: 14px;
}
.action-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 10px;
}
.action-card {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 11px;
  background: var(--subtle);
  color: var(--text);
  text-decoration: none;
  transition:
    border-color 0.15s,
    transform 0.15s;
}
.action-card:hover {
  border-color: var(--accent);
  transform: translateY(-1px);
}
.action-card svg {
  color: var(--accent);
  margin-top: 2px;
}
.action-text {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 13px;
}
.action-text small {
  color: var(--muted);
  line-height: 1.5;
}
.save-row {
  position: sticky;
  bottom: 12px;
  margin-top: 14px;
  padding: 10px 12px;
  align-items: center;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--panel-solid);
  box-shadow: var(--shadow);
}
.dirty-hint {
  color: var(--warning);
  font-size: 12.5px;
}
</style>
