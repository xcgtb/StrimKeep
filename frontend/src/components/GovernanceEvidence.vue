<script setup lang="ts">
import { computed } from 'vue';
import { object, strings, records, type Data } from '../api/domain';
import EvidenceDetails from './EvidenceDetails.vue';
const props = defineProps<{ value: Data }>();
const meta = computed(() => object(props.value.meta));
const label = computed(() => String(props.value.title || meta.value.title || props.value.text || '治理条目'));
const sources = computed(() => records(meta.value.cloud_sources));
const localPaths = computed(() => strings(meta.value.local_paths));
const sharePaths = computed(() => strings(meta.value.share_paths));
const reason = computed(() => String(props.value.reason_label || meta.value.reason_label || props.value.detail || '已记录治理决策'));
</script>
<template>
  <div class="governance-evidence">
    <h3>{{ label }} <span v-if="meta.season != null">· S{{ String(meta.season).padStart(2, '0') }}</span></h3>
    <p class="notice-bar"><strong>决策依据：</strong>{{ reason }}</p>
    <p v-if="value.detail && value.detail !== reason">{{ value.detail }}</p>
    <div class="evidence-facts">
      <div><span>匹配来源</span><strong>{{ meta.match_basis || '依当前治理匹配规则' }}</strong></div>
      <div><span>处理 STRM</span><strong>{{ value.files_count ?? meta.member_count ?? '—' }}</strong></div>
      <div><span>本地关联</span><strong>{{ meta.local_count ?? localPaths.length ?? '—' }}</strong></div>
      <div><span>分享关联</span><strong>{{ meta.share_count ?? sharePaths.length ?? '—' }}</strong></div>
    </div>
    <p v-if="meta.match_basis_detail" class="muted-text">{{ meta.match_basis_detail }}</p>
    <p v-if="meta.tmdb_consistent === false" class="notice-bar warning-text">两侧 TMDB 标记存在冲突，需核对作品身份；此处不是自动删除授权。</p>
    <details v-if="localPaths.length" open><summary>本地 STRM 路径（{{ localPaths.length }}）</summary>
      <div class="evidence-path-list"><code v-for="path in localPaths" :key="path">{{ path }}</code></div>
    </details>
    <details v-if="sharePaths.length" open><summary>分享 STRM 路径（{{ sharePaths.length }}）</summary>
      <div class="evidence-path-list"><code v-for="path in sharePaths" :key="path">{{ path }}</code></div>
    </details>
    <details v-if="sources.length"><summary>115 来源身份与官方删除核验（{{ sources.length }}）</summary><EvidenceDetails :value="sources" /></details>
    <details><summary>七维比较、保护策略及完整元数据</summary><EvidenceDetails :value="value" /></details>
  </div>
</template>
