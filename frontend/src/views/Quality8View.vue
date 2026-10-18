<script setup lang="ts">
import { computed } from 'vue';
import { records } from '../api/domain';
import { useCloudSettingsDraft } from '../composables/useCloudSettingsDraft';
import defaults from '../config/quality-defaults.json';
import PageHeading from '../components/PageHeading.vue';
import WorkflowRail from '../components/WorkflowRail.vue';
import QualityRules from '../components/QualityRules.vue';
import FormFields from '../components/FormFields.vue';
import OperationState from '../components/OperationState.vue';
import CloudSettingsConfirmation from '../components/CloudSettingsConfirmation.vue';
const { request, op, draft, confirmation, changed, changes, load, save } = useCloudSettingsDraft();
const rules = computed(() => records(draft.value.quality8_rules));
function resetQuality() {
  draft.value = {
    ...draft.value,
    quality8_rules: [
      ...defaults.cover.rules.map((r) => ({
        key: r.key === 'dolby' ? 'dynamic_range' : r.key,
        enabled: r.enabled,
        tiers: structuredClone(r.tiers),
        groups: structuredClone(r.groups),
      })),
      { key: 'size', enabled: true, tiers: [], groups: [] },
    ],
  };
}
</script>
<template>
  <PageHeading
    title="整理八维与洗版"
    subtitle="按真实视频属性比较同作品版本，独立管理整理阶段的质量与保留策略。"
    eyebrow="媒体加工"
    ><button class="button" :disabled="op.busy.value" @click="load">重新读取</button
    ><button
      class="button primary"
      :disabled="!changed.length || op.busy.value || op.uncertain.value || !!request.error.value"
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
  <div v-if="request.data.value" class="primary-side-layout">
    <section class="panel">
      <header class="section-heading">
        <div>
          <h2>整理八维质量规则</h2>
          <p>从上到下逐维比较，关闭的维度会跳过。</p>
        </div>
        <span class="module-tag">{{ rules.filter((r) => r.enabled).length }} / 8 已启用</span>
      </header>
      <p class="notice-bar">
        整理阶段读取 115 原视频的真实大小。生成后的 STRM 由独立七维规则治理。
      </p>
      <FormFields
        v-model="draft"
        :disabled="op.busy.value || op.uncertain.value"
        :fields="[
          { key: 'quality8_enabled', label: '启用整理八维比较', type: 'check' },
          {
            key: 'quality8_mode',
            label: '版本处理模式',
            type: 'select',
            options: [
              { value: 'preview', label: '只预览建议' },
              { value: 'upgrade', label: '按已保存策略执行洗版' },
            ],
          },
          { key: 'quality8_coexist_dv', label: '杜比与非杜比版本共存', type: 'check' },
        ]"
      />
      <h3 class="detail-section-title">八维排序与优先级</h3>
      <QualityRules
        :model-value="rules"
        :disabled="op.busy.value || op.uncertain.value"
        @update:model-value="(v) => (draft.quality8_rules = v)"
      />
      <div class="action-row">
        <button
          class="button"
          :disabled="op.busy.value || op.uncertain.value"
          @click="resetQuality"
        >
          恢复八维默认草稿
        </button>
      </div>
    </section>
    <aside class="panel stage-guide">
      <h2>整理链路的配置关系</h2>
      <RouterLink to="/settings/cloud?section=quality"
        ><strong>源文件治理授权</strong><span>核查删除权限与源文件登记 →</span></RouterLink
      >
      <RouterLink to="/organize/naming"
        ><strong>自定义命名模板</strong><span>目录、文件名与季集格式 →</span></RouterLink
      ><RouterLink to="/governance/rules"
        ><strong>七维治理策略</strong><span>本地与分享 STRM 的独立规则 →</span></RouterLink
      ><RouterLink to="/organize/workspace"
        ><strong>来源与归档目录</strong><span>核对当前账号及 115 目录 →</span></RouterLink
      ><RouterLink :to="{ path: '/settings/cloud', query: { section: 'output' } }"
        ><strong>STRM 输出映射</strong><span>NAS 与 Emby 看到的实际位置 →</span></RouterLink
      >
      <p class="data-caption">恢复默认仅修改当前草稿，保存后才会用于后续任务。</p>
    </aside>
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
