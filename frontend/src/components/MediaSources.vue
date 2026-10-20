<script setup lang="ts">
import { onMounted, computed, ref } from 'vue';
import { readData, records, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { date, number } from '../composables/format';
const props = defineProps<{ snapshot?: Data; compact?: boolean }>();
const expanded = ref(false);
const request = useRequest<Data>();
const data = computed(() => props.snapshot || request.data.value);
const items = computed(() => records(data.value?.items));
function load() {
  return request.run((signal) => readData('/api/library/sources', signal));
}
onMounted(() => {
  if (!props.snapshot) void load();
});
</script>
<template>
  <section class="panel media-source-panel" aria-label="媒体来源">
    <header class="section-heading">
      <div>
        <h2>媒体来源</h2>
        <p class="muted-text">上游与自建同时接入，个人库与分享库分别汇总。</p>
      </div>
      <div class="action-row">
        <button v-if="compact" class="button small" @click="expanded = !expanded">
          {{ expanded ? '收起来源路径' : '展开来源路径' }}</button
        ><RouterLink class="text-link" to="/settings">配置 Emby 路径 →</RouterLink>
      </div>
    </header>
    <p v-if="request.error.value" class="inline-error">
      {{ request.error.value }} <button class="button small" @click="load">重新读取</button>
    </p>
    <p v-if="object(data?.scan).error" class="warning-text">{{ object(data?.scan).error }}</p>
    <div class="media-source-grid" :class="{ compact: compact && !expanded }">
      <article v-for="source in items" :key="String(source.id)" class="media-source-card">
        <div class="section-heading">
          <strong>{{ source.label }}</strong
          ><span class="module-tag">{{ source.origin === 'generated' ? '自建' : '上游' }}</span>
        </div>
        <p class="media-source-count">
          {{ source.count == null ? '待核验' : number(source.count)
          }}<small v-if="source.count != null"> 个 STRM</small>
        </p>
        <template v-if="!compact || expanded">
          <p class="source-path">
            <small>StrimKeep 目录</small><code>{{ source.root }}</code>
          </p>
          <p class="source-path">
            <small>Emby 目录</small><code>{{ source.emby_root || '尚未配置' }}</code>
          </p>
          <p v-if="source.scan_error || source.message" class="warning-text">
            {{ source.scan_error || source.message }}
          </p>
          <p v-if="Number(source.duplicates)" class="muted-text">
            {{ source.duplicates }} 个重复文件已排除
          </p>
          <p class="muted-text">
            {{ source.governance ? '已纳入治理扫描范围' : '自建治理联动未开启' }} ·
            {{ date(Number(source.verified_ts)) }}
          </p></template
        >
      </article>
    </div>
    <p v-if="!compact || expanded" class="muted-text">
      统计显示最近核验结果；确认挂载正确后，可在媒体总览点击「刷新总览」重建统计。同一实际文件只计一次，不按播放链接合并不同文件。
    </p>
    <RouterLink v-if="!compact || expanded" class="text-link" to="/settings/cloud?section=quality"
      >配置自建治理联动与源文件删除权限 →</RouterLink
    >
  </section>
</template>
