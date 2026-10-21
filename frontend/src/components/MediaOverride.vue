<script setup lang="ts">
import { ref, watch } from 'vue';
import { query, readData, records, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import ModalDialog from './ModalDialog.vue';
import EvidenceDetails from './EvidenceDetails.vue';
const props = defineProps<{ fileId: string; name?: string; initial?: Data }>();
const emit = defineEmits<{ close: []; save: [value: { id: string; choice: Data }] }>();
const search = ref(String(props.initial?.title || props.name || '')),
  kind = ref(String(props.initial?.kind || 'movie')),
  year = ref(String(props.initial?.year || '')),
  season = ref<number | undefined>(props.initial?.season as number | undefined),
  episode = ref<number | undefined>(props.initial?.episode as number | undefined),
  category = ref(String(props.initial?.category_override || '')),
  choice = ref<Data | undefined>(props.initial?.tmdb_id ? props.initial : undefined),
  request = useRequest<Data>();
watch([search, kind, year], () => {
  request.cancel();
  request.data.value = undefined;
  choice.value = undefined;
});
function load() {
  choice.value = undefined;
  return request.run((signal) =>
    readData(
      query('/api/cloud/tmdb', { query: search.value, kind: kind.value, year: year.value }),
      signal,
    ),
  );
}
function save() {
  if (!choice.value) return;
  emit('save', {
    id: props.fileId,
    choice: {
      kind: kind.value,
      tmdb_id: String(choice.value.tmdb_id || choice.value.id),
      ...(kind.value === 'tv' && Number.isInteger(season.value) ? { season: season.value } : {}),
      ...(kind.value === 'tv' && Number.isInteger(episode.value) ? { episode: episode.value } : {}),
      ...(category.value ? { category: category.value } : {}),
    },
  });
}
</script>
<template>
  <ModalDialog title="修正影视识别" @close="$emit('close')"
    ><p class="muted-text">
      修正用于当前页面的本批预览；核对并执行归档后才保存媒体身份。不会自动修改其他文件或定时任务。
    </p>
    <form class="filter-panel" @submit.prevent="load">
      <label>片名<input v-model="search" minlength="2" required /></label
      ><label
        >类型<select v-model="kind">
          <option value="movie">电影</option>
          <option value="tv">剧集</option>
        </select></label
      ><label>年份<input v-model="year" placeholder="可留空" /></label
      ><button class="button">搜索 TMDB</button>
    </form>
    <p v-if="request.error.value" class="inline-error">{{ request.error.value }}</p>
    <div class="candidate-list">
      <button
        v-for="row in records(request.data.value?.items)"
        :key="String(row.id || row.tmdb_id)"
        :class="{ selected: choice?.id === row.id }"
        @click="choice = row"
      >
        <strong>{{ row.title || row.name }}</strong
        ><span
          >{{ row.year || row.release_date || row.first_air_date }} · TMDB
          {{ row.tmdb_id || row.id }}</span
        >
      </button>
    </div>
    <div v-if="choice" class="form-grid">
      <p>
        当前选择：{{ choice.title || choice.name || 'TMDB 作品' }} · TMDB
        {{ choice.tmdb_id || choice.id }}
      </p>
      <label v-if="kind === 'tv'"
        >指定季号<input v-model.number="season" type="number" min="0" max="99" /></label
      ><label v-if="kind === 'tv'"
        >指定集号<input v-model.number="episode" type="number" min="1" max="999" /></label
      ><label>指定分类（可留空）<input v-model="category" /></label>
    </div>
    <details v-if="choice">
      <summary>候选元数据</summary>
      <EvidenceDetails :value="choice" />
    </details>
    <template #footer
      ><button class="button primary" :disabled="!choice" @click="save">
        采用此识别并重新预览
      </button></template
    ></ModalDialog
  >
</template>
