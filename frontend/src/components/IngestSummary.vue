<script setup lang="ts">
import { computed } from 'vue';
import { Clapperboard, Film, Clock3, FolderOpen } from '@lucide/vue';
import { object, type Data } from '../api/domain';
import { number, date } from '../composables/format';

const props = defineProps<{ value?: Data }>();
const data = computed(() => object(props.value));
const stats = computed(() => object(data.value.stats));
const tree = computed(() => object(data.value.tree));
type Group = {
  source: string;
  category: string;
  kind: 'tv' | 'movie';
  shows: { title: string; count?: number; seasons?: string }[];
};
const groups = computed<Group[]>(() => {
  const result: Group[] = [];
  for (const kind of ['tv', 'movie'] as const) {
    const branches = object(tree.value[kind]);
    const details = object(tree.value[kind === 'tv' ? 'tv_detail' : 'mov_detail']);
    const sources = [
      '本地影视库',
      '分享影视库',
      ...Object.keys(branches).filter((s) => !['本地影视库', '分享影视库'].includes(s)),
    ];
    for (const source of [...new Set(sources)]) {
      for (const [category, raw] of Object.entries(object(branches[source]))) {
        const items: Group['shows'] = [];
        if (kind === 'movie' && Array.isArray(raw)) {
          for (const name of raw) if (typeof name === 'string') items.push({ title: name });
        } else {
          for (const [title, count] of Object.entries(object(raw))) {
            const detail = object(object(object(details[source])[category])[title]);
            const seasons = Object.entries(object(detail.seasons))
              .map(([season, v]) => {
                const info = object(v);
                return `S${String(season).padStart(2, '0')} · ${number(info.count)} 集`;
              })
              .join(' / ');
            items.push({ title, count: Number(count) || 0, seasons });
          }
        }
        if (items.length) result.push({ source, category, kind, shows: items });
      }
    }
  }
  return result;
});
</script>
<template>
  <div class="ingest-report">
    <p v-if="data.warning" class="notice-bar" role="alert">
      Emby 读取提示：{{ data.warning }}。当前可能是上次成功的缓存。
    </p>
    <div class="ingest-metrics">
      <article>
        <Film :size="19" /><span>新增电影</span><strong>{{ number(stats.movies) }}</strong
        ><small>部 · 按作品去重</small>
      </article>
      <article>
        <Clapperboard :size="19" /><span>新增剧集</span><strong>{{ number(stats.series) }}</strong
        ><small>部 · 按作品去重</small>
      </article>
      <article>
        <FolderOpen :size="19" /><span>新增单集</span><strong>{{ number(stats.episodes) }}</strong
        ><small>集 · 按季集去重</small>
      </article>
    </div>
    <div class="ingest-time">
      <Clock3 :size="14" />近 24 小时 Emby 新增 · 缓存更新 {{ date(data.cache_ts as number)
      }}<span v-if="data.from_cache"> · 缓存结果</span>
    </div>
    <div v-if="groups.length" class="ingest-groups">
      <details
        v-for="(group, index) in groups"
        :key="`${group.kind}:${group.source}:${group.category}`"
        class="ingest-group"
        :open="index < 2"
      >
        <summary>
          <span class="ingest-group-title"
            >{{ group.kind === 'tv' ? '剧集' : '电影' }} · {{ group.source }} /
            {{ group.category }}</span
          ><span class="muted-text">{{ group.shows.length }} 部</span>
        </summary>
        <div class="ingest-shows">
          <article v-for="(show, i) in group.shows" :key="`${show.title}:${i}`">
            <div>
              <strong>{{ show.title }}</strong
              ><small v-if="show.seasons">{{ show.seasons }}</small>
            </div>
            <span v-if="group.kind === 'tv'" class="ingest-episode-pill"
              >{{ number(show.count) }} 集</span
            >
          </article>
        </div>
      </details>
    </div>
    <p v-else class="activity-empty">
      当前统计窗口暂无新增电影或剧集。若 Emby 刚入库，请先确认增量监控与扫描状态。
    </p>
  </div>
</template>
