<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Film, Check } from '@lucide/vue';
import type { MediaCard } from '../types/api';
import { posterUrl, titleOf, badge, episodeSummary } from '../composables/format';
const props = defineProps<{
  card: MediaCard;
  selectable?: boolean;
  selected?: boolean;
  priority?: boolean;
  compact?: boolean;
  hideStatus?: boolean;
}>();
defineEmits<{ open: []; select: [] }>();
const status = computed(() => badge(props.card));
const statusTone = computed(() => {
  if (props.card.tmdb_info?.match_status === 'missing' || status.value.startsWith('已入库 '))
    return 'warning';
  if (status.value.startsWith('超')) return 'extra';
  if (
    ['未入库', '片库待同步', '集数待同步', '身份冲突', '待对照', '未匹配', '对照中'].some((s) =>
      status.value.startsWith(s),
    )
  )
    return 'unknown';
  return 'complete';
});
const loaded = ref(false),
  failed = ref(false),
  url = computed(() => posterUrl(props.card));
watch(url, () => {
  failed.value = false;
  loaded.value = false;
});
</script>
<template>
  <article
    class="poster-card"
    :class="{ selected, selectable, 'media-compact': compact }"
    :data-media-id="card.id"
  >
    <div class="poster-art" :class="{ 'poster-waiting': url && !loaded && !failed }">
      <button
        class="poster-open"
        :aria-label="`${selectable ? (selected ? '取消选择' : '选择') : '查看'}${titleOf(card)}${selectable ? '海报' : '详情'}`"
        @click="selectable ? $emit('select') : $emit('open')"
      >
        <img
          v-if="url && !failed"
          :src="url"
          alt=""
          :loading="priority ? 'eager' : 'lazy'"
          :fetchpriority="priority ? 'high' : 'auto'"
          decoding="async"
          @load="loaded = true"
          @error="failed = true"
        /><span v-else class="poster-fallback"
          ><Film :size="32" /><span>{{ titleOf(card) }}</span></span
        ></button
      ><span v-if="card.type" class="poster-type">{{ card.type === 'tv' ? '剧集' : '电影' }}</span
      ><span v-if="card.rating && !selectable" class="rating">★ {{ card.rating.toFixed(1) }}</span
      ><span
        v-if="status && !hideStatus"
        class="poster-status"
        :class="statusTone"
        :title="card.facts_stale ? '片库缓存中的集数，TMDB 对照已过期，等待同步' : status"
        >{{ status }}</span
      ><button
        v-if="selectable"
        class="selection-button"
        :class="{ checked: selected }"
        :aria-label="`${selected ? '取消选择' : '选择'}${titleOf(card)}`"
        :aria-pressed="!!selected"
        @click.stop="$emit('select')"
      >
        <Check v-if="selected" :size="15" />
      </button>
    </div>
    <div class="poster-caption">
      <button class="poster-title" @click="selectable ? $emit('select') : $emit('open')">
        {{ titleOf(card) }}
      </button>
      <div class="poster-meta">
        <span>{{ card.year || '年份未知' }}</span
        ><span v-if="card.type">{{ card.type === 'tv' ? '剧集' : '电影' }}</span>
      </div>
      <p
        v-if="!compact && episodeSummary(card)"
        class="poster-episodes"
        :title="episodeSummary(card)"
        :aria-hidden="!episodeSummary(card)"
      >
        {{ episodeSummary(card) }}
      </p>
      <div
        v-if="card.source_labels?.length || card.in_local || card.in_share"
        class="library-tags"
        :aria-hidden="!card.source_labels?.length && !card.in_local && !card.in_share"
      >
        <template v-if="card.source_labels?.length"
          ><span v-for="source in card.source_labels" :key="source">{{ source }}</span></template
        >
        <template v-else
          ><span v-if="card.in_local">本地库</span
          ><span v-if="card.in_share">分享库</span></template
        >
      </div>
    </div>
  </article>
</template>
