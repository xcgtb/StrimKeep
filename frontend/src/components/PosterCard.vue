<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { Film, Check, Plus } from '@lucide/vue';
import type { MediaCard } from '../types/api';
import { posterUrl, titleOf, badge } from '../composables/format';
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
const openLabel = computed(
  () =>
    `${props.selectable ? (props.selected ? '取消选择' : '选择') : '查看'}${titleOf(props.card)}${props.selectable ? '' : '详情'}`,
);
const loaded = ref(false),
  failed = ref(false),
  url = computed(() => posterUrl(props.card));
watch(url, () => {
  failed.value = false;
  loaded.value = false;
});
</script>
<template>
  <article class="poster-card" :class="{ selected, selectable }">
    <div class="poster-art" :class="{ 'poster-waiting': url && !loaded && !failed }">
      <button
        class="poster-open"
        :aria-label="openLabel"
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
      ><span v-if="card.rating" class="rating">★ {{ card.rating.toFixed(1) }}</span
      ><span
        v-if="badge(card)"
        class="poster-status"
        :class="{ warning: card.tmdb_info?.match_status === 'missing' }"
        >{{ badge(card) }}</span
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
        ><span v-if="card.type">{{ card.type === 'tv' ? '剧集' : '电影' }}</span
        ><span v-if="card.have_eps != null">{{ card.have_eps }} 集</span>
      </div>
      <div v-if="card.in_local || card.in_share" class="library-tags">
        <span v-if="card.in_local">本地库</span><span v-if="card.in_share">分享库</span>
      </div>
      <button
        v-if="subscribable"
        class="subscribe-button"
        :class="{ active: subscribed }"
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
