<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Film, Check, Plus, Star } from '@lucide/vue';
import type { MediaCard } from '../types/api';
import { posterUrl, titleOf } from '../composables/format';

const props = defineProps<{
  card: MediaCard;
  selectable?: boolean;
  selected?: boolean;
  subscribable?: boolean;
  subscribed?: boolean;
  busy?: boolean;
  priority?: boolean;
}>();
defineEmits<{ open: []; select: []; subscribe: [] }>();

const loaded = ref(false),
  failed = ref(false),
  url = computed(() => posterUrl(props.card));
watch(url, () => {
  failed.value = false;
  loaded.value = false;
});

const typeLabel = computed(() => (props.card.type === 'tv' ? '剧集' : '电影'));

// classic renderSeriesCard 状态映射
const status = computed(() => {
  const info = props.card.tmdb_info;
  const mst = info?.match_status || 'unmatched';
  const map: Record<string, { text: string; cls: string }> = {
    aligned: { text: '✓ 完整', cls: 'ok' },
    missing: { text: `缺 ${Math.abs(info?.diff || 0)} 集`, cls: 'local' },
    extra: { text: `超 ${info?.diff || 0} 集`, cls: 'share' },
    ongoing: { text: '⏳ 在更', cls: 'ok' },
    unmatched: { text: '未匹配', cls: 'no' },
    no_tmdb: { text: '无 TMDB', cls: 'no' },
    pending: { text: '待对照', cls: 'no' },
  };
  return map[mst] || { text: '', cls: 'no' };
});

const libTag = computed(() => {
  const c = props.card;
  if (c.in_local && c.in_share) return '双库';
  if (c.in_local) return '本地';
  if (c.in_share) return '分享';
  return '';
});

const episodeCount = computed(() => {
  const c = props.card;
  const have = c.have_eps ?? c.local_eps ?? c.share_eps;
  return have != null ? Number(have) : 0;
});
const tmdbTotal = computed(() => {
  const c = props.card;
  return Number(c.tmdb_info?.tmdb_total ?? c.total_episodes ?? 0);
});
// classic renderSeriesCard 集数信息
const epInfo = computed(() => {
  const c = props.card;
  if (tmdbTotal.value > 0) return `已入库 ${episodeCount.value}/${tmdbTotal.value} 集`;
  if (c.in_local && c.in_share) return `本地 ${c.local_eps ?? 0} / 分享 ${c.share_eps ?? 0} 集`;
  return `${c.total_seasons ?? 0} 季 · ${episodeCount.value} 集`;
});
</script>
<template>
  <article class="poster-card emby-poster-card" :class="{ selected }">
    <div class="poster-wrap" :class="{ 'poster-waiting': url && !loaded && !failed }">
      <button class="poster-open" :aria-label="`查看${titleOf(card)}详情`" @click="$emit('open')">
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
        >
      </button>
      <span class="badge-type">{{ typeLabel }}</span>
      <span v-if="card.rating" class="badge-rate"><Star :size="10" fill="currentColor" />{{
        card.rating.toFixed(1)
      }}</span>
      <span v-if="status.text" class="badge-status" :class="status.cls">{{ status.text }}</span>
      <button
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
    <div class="info">
      <button class="poster-title t" @click="$emit('open')">{{ titleOf(card) }}</button>
      <div class="meta">
        <span>{{ card.year || '—' }}<template v-if="libTag"> · {{ libTag }}</template></span>
        <span v-if="card.type">{{ typeLabel }}</span>
      </div>
      <div v-if="epInfo" class="meta"><span style="color: var(--text-dim)">{{ epInfo }}</span></div>
      <button
        v-if="subscribable"
        class="sub-btn"
        :class="{ on: subscribed }"
        :disabled="busy"
        @click.stop="$emit('subscribe')"
      >
        <Check v-if="subscribed" :size="13" /><Plus v-else :size="13" />{{
          subscribed ? '已订阅' : '追更订阅'
        }}
      </button>
    </div>
  </article>
</template>
