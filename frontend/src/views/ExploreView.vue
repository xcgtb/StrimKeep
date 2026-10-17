<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { get, post } from '../api/client';
import { query, records, text } from '../api/domain';

const regions = [
  { value: 'all', label: '全部' },
  { value: 'cn', label: '大陆' },
  { value: 'hk', label: '香港' },
  { value: 'tw', label: '台湾' },
  { value: 'jp', label: '日本' },
  { value: 'kr', label: '韩国' },
  { value: 'us', label: '欧美' },
];
const medias = [
  { value: 'movie', label: '电影' },
  { value: 'tv', label: '剧集' },
  { value: 'all', label: '全部' },
];
const sorts = [
  { value: 'popularity', label: '热度' },
  { value: 'release', label: '最新' },
  { value: 'rating', label: 'TMDB 评分' },
];
const genres = [
  '动作', '冒险', '喜剧', '犯罪', '纪录', '剧情', '家庭', '奇幻', '历史', '恐怖',
  '音乐', '悬疑', '爱情', '科幻', '惊悚', '战争', '西部', '动画', '儿童', '新闻',
  '真人秀', '肥皂剧', '脱口秀', '电视电影',
];

const filters = ref({ region: 'all', media: 'movie', sort: 'popularity', genre: '', year: '' });
const keyword = ref('');
const cards = ref<Record<string, unknown>[]>([]);
const page = ref(1);
const totalPages = ref(1);
const loading = ref(false);
const error = ref('');
const expanded = ref(false);
const yearTop = new Date().getFullYear() + 1;
const yearCount = ref(30);

const years = computed(() => {
  const list: string[] = ['全部'];
  for (let i = 0; i < yearCount.value; i += 1) {
    const year = yearTop - i;
    if (year < 1900) break;
    list.push(String(year));
  }
  return list;
});

async function load(more = false) {
  loading.value = true;
  error.value = '';
  const target = more ? page.value + 1 : 1;
  try {
    const data = await get<Record<string, unknown>>(
      query('/api/explore', { ...filters.value, q: keyword.value, page: target }),
    );
    const incoming = records(data.cards);
    cards.value = more ? [...cards.value, ...incoming] : incoming;
    page.value = target;
    totalPages.value = Number(data.total_pages || 1);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
function pick(key: keyof typeof filters.value, value: string) {
  filters.value = { ...filters.value, [key]: value };
  void load();
}
function loadMoreYears() {
  if (yearTop - yearCount.value < 1900) return;
  yearCount.value += 20;
}
function badge(card: Record<string, unknown>) {
  if (card.in_emby) return '已入库';
  if (card.in_local) return '本地库';
  if (card.in_share) return '分享库';
  return '';
}
const subscribed = ref<Set<string>>(new Set());
const subList = ref<Record<string, unknown>[]>([]);
async function loadSubscriptions() {
  try {
    const data = await get<Record<string, unknown>>('/api/subscriptions');
    subList.value = records(data.subscriptions);
    subscribed.value = new Set(subList.value.map((s) => text(s.tmdb_id)));
  } catch {
    /* 忽略 */
  }
}
async function toggleSubscribe(card: Record<string, unknown>) {
  if (card.type !== 'tv' || !card.tmdb_id) return;
  const id = text(card.tmdb_id);
  const next = new Set(subscribed.value);
  let list: Record<string, unknown>[];
  if (next.has(id)) {
    next.delete(id);
    list = subList.value.filter((s) => text(s.tmdb_id) !== id);
  } else {
    next.add(id);
    list = [
      ...subList.value,
      { id, tmdb_id: id, name: text(card.title), poster: text(card.poster), enabled: true },
    ];
  }
  try {
    await post('/api/subscriptions', { subscriptions: list });
    subscribed.value = next;
    subList.value = list;
  } catch (e) {
    error.value = (e as Error).message;
  }
}
watch(keyword, () => void load());
onMounted(() => {
  void load();
  void loadSubscriptions();
});
</script>
<template>
  <section class="tab active">
    <div class="filter-summary">
      <span>{{ regions.find((r) => r.value === filters.region)?.label }} · {{ medias.find((m) => m.value === filters.media)?.label }} · {{ filters.year || '全部年份' }} · {{ sorts.find((s) => s.value === filters.sort)?.label }}</span>
      <button type="button" @click="expanded = !expanded">{{ expanded ? '收起筛选' : '展开筛选' }}</button>
    </div>
    <div v-if="expanded" class="filters">
      <div class="filter-row">
        <span class="filter-label">地区</span>
        <div class="filter-chips">
          <button v-for="r in regions" :key="r.value" class="chip" :class="{ active: filters.region === r.value }" @click="pick('region', r.value)">{{ r.label }}</button>
        </div>
      </div>
      <div class="filter-row">
        <span class="filter-label">类型</span>
        <div class="filter-chips">
          <button v-for="m in medias" :key="m.value" class="chip" :class="{ active: filters.media === m.value }" @click="pick('media', m.value)">{{ m.label }}</button>
        </div>
      </div>
      <div class="filter-row">
        <span class="filter-label">题材</span>
        <div class="filter-chips">
          <button class="chip" :class="{ active: !filters.genre }" @click="pick('genre', '')">全部</button>
          <button v-for="g in genres" :key="g" class="chip" :class="{ active: filters.genre === g }" @click="pick('genre', g)">{{ g }}</button>
        </div>
      </div>
      <div class="filter-row">
        <span class="filter-label">年份</span>
        <div class="filter-chips year-scroll" @scroll="(e) => { const t = e.target as HTMLElement; if (t.scrollLeft + t.clientWidth >= t.scrollWidth - 80) loadMoreYears(); }">
          <button v-for="y in years" :key="y" class="chip" :class="{ active: (filters.year || '全部') === y }" @click="pick('year', y === '全部' ? '' : y)">{{ y }}</button>
        </div>
      </div>
      <div class="filter-row">
        <span class="filter-label">排序</span>
        <div class="filter-chips">
          <button v-for="s in sorts" :key="s.value" class="chip" :class="{ active: filters.sort === s.value }" @click="pick('sort', s.value)">{{ s.label }}</button>
        </div>
      </div>
    </div>
    <div class="search-hero">
      <input v-model="keyword" type="text" placeholder="搜索 TMDB 电影/剧集，如「三体」「Oppenheimer」" @keyup.enter="load()" />
      <button class="search-btn" data-icon="search" @click="load()">搜索</button>
    </div>
    <p v-if="error" class="section-note">{{ error }}</p>
    <div class="poster-grid">
      <article v-for="card in cards" :key="text(card.type) + text(card.tmdb_id)" class="poster-card">
        <div class="poster-wrap">
          <img v-if="card.poster" :src="text(card.poster)" :alt="text(card.title)" loading="lazy" />
          <span class="badge-type">{{ card.type === 'tv' ? '剧集' : '电影' }}</span><span v-if="card.rating" class="badge-rate">{{ card.rating }}</span><span v-if="badge(card)" class="badge-status ok">{{ badge(card) }}</span>
        </div>
        <div class="info"><div class="t">{{ text(card.title) }}</div>
        <div class="meta"><span>{{ text(card.year) }}</span><button
          v-if="card.type === 'tv'"
          class="sub-btn"
          :class="{ on: subscribed.has(text(card.tmdb_id)) }"
          @click="toggleSubscribe(card)"
        >{{ subscribed.has(text(card.tmdb_id)) ? '已订阅' : '追更订阅' }}</button></div></div>
      </article>
    </div>
    <div v-if="!cards.length && !loading" class="list-empty">没有找到相关作品</div>
    <div v-if="page < totalPages" class="load-more-footer">
      <button class="btn" :disabled="loading" @click="load(true)">{{ loading ? '正在加载…' : '加载更多' }}</button>
    </div>
  </section>
</template>
