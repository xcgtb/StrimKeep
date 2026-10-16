<script setup lang="ts">
import { ref, watch } from 'vue';
import { query, readData, records, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import ModalDialog from './ModalDialog.vue';
import EvidenceDetails from './EvidenceDetails.vue';
const props = defineProps<{ fileId: string; name?: string }>();
const emit = defineEmits<{ close: []; save: [value: { id: string; choice: Data }] }>();
const search = ref(props.name || ''),
  kind = ref('movie'),
  year = ref(''),
  season = ref<number | undefined>(),
  episode = ref<number | undefined>(),
  category = ref(''),
  choice = ref<Data>(),
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
      ...(kind.value === 'tv' && season.value !== undefined ? { season: season.value } : {}),
      ...(kind.value === 'tv' && episode.value !== undefined ? { episode: episode.value } : {}),
      ...(category.value ? { category: category.value } : {}),
    },
  });
}
</script>
<template>
  <ModalDialog title="修正影视识别" @close="$emit('close')"
    ><form class="filter-panel" @submit.prevent="load">
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
