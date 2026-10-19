<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import { accountPath, readData, writeData, query, records, object, type Data } from '../api/domain';
import type { MediaCard } from '../types/api';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { date } from '../composables/format';
import DirectoryPicker from './DirectoryPicker.vue';
import LoadState from './LoadState.vue';
import OperationState from './OperationState.vue';
import TaskReceipt from './TaskReceipt.vue';
import PosterCard from './PosterCard.vue';
import ModalDialog from './ModalDialog.vue';

const props = defineProps<{ accountId: string }>();
const emit = defineEmits<{ browse: [value: { cid: string; name: string }] }>();
const catalogue = useRequest<Data>(),
  op = useOperation(() => props.accountId);
const cid = ref('0'),
  name = ref('全部文件'),
  kindHint = ref('auto'),
  enrich = ref(true),
  filter = ref(''),
  keyword = ref(''),
  appliedKeyword = ref(''),
  offset = ref(0),
  job = ref(''),
  jobState = ref<Data>(),
  selected = ref<Data>();
const items = computed(() => records(catalogue.data.value?.items));
const summary = computed(() => object(catalogue.data.value?.summary));
const busyJob = computed(() => ['queued', 'running'].includes(String(jobState.value?.state)));
const counts = computed(() => object(catalogue.data.value?.counts));
function card(row: Data): MediaCard {
  return {
    id: String(row.cid),
    title: String(row.title),
    year: Number(row.year) || undefined,
    type: row.kind === 'movie' || row.kind === 'tv' ? row.kind : undefined,
    poster: String(row.poster || ''),
  };
}
const kindLabel = (kind: unknown) =>
  kind === 'movie' ? '电影' : kind === 'tv' ? '剧集' : '类型待确认';
function size(value: unknown) {
  const bytes = Number(value) || 0;
  return bytes >= 1024 ** 3
    ? `${(bytes / 1024 ** 3).toFixed(1)} GB`
    : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}
async function load() {
  if (!props.accountId) return;
  const aid = props.accountId;
  const result = await catalogue.run((signal) =>
    readData(
      query(accountPath(aid, 'inventory'), {
        root_cid: cid.value,
        kind: filter.value,
        keyword: appliedKeyword.value,
        offset: offset.value,
        limit: 48,
      }),
      signal,
    ),
  );
  if (result && aid === props.accountId && result.active_job && !job.value)
    job.value = String(result.active_job);
}
async function scan() {
  const result = await op.run(() =>
    writeData(accountPath(props.accountId, 'inventory/scan'), {
      root_cid: cid.value,
      kind_hint: kindHint.value,
      enrich: enrich.value,
    }),
  );
  if (result) {
    job.value = String(result.job_id);
    jobState.value = { state: 'queued' };
  }
}
async function stop() {
  await op.run(() => writeData(`/api/cloud/jobs/${job.value}/stop`, { confirmed: true }));
}
function taskUpdated(value: Data) {
  const previous = jobState.value?.state;
  jobState.value = value;
  if (value.state === 'success' && previous !== 'success') void load();
}
function chooseRoot(value: { id: string; name: string }) {
  name.value = value.name;
  cid.value = value.id;
}
function search() {
  appliedKeyword.value = keyword.value.trim();
  offset.value = 0;
  void load();
}
watch([cid, filter], () => {
  catalogue.cancel();
  selected.value = undefined;
  offset.value = 0;
  job.value = '';
  jobState.value = undefined;
  void load();
});
watch(
  () => props.accountId,
  () => {
    catalogue.cancel();
    catalogue.data.value = undefined;
    selected.value = undefined;
    cid.value = '0';
    name.value = '全部文件';
    filter.value = '';
    keyword.value = '';
    appliedKeyword.value = '';
    offset.value = 0;
    job.value = '';
    jobState.value = undefined;
    op.invalidate();
    void load();
  },
  { immediate: true },
);
useVisiblePoll(() => (!op.busy.value ? load() : undefined), 10000);
</script>

<template>
  <section class="panel inventory-controls">
    <header class="section-heading">
      <div>
        <h2>盘内媒体库存</h2>
        <p>查看 115 已有作品目录；扫描不会重命名、移动或删除网盘文件。</p>
      </div>
      <span class="module-tag">只读扫描</span>
    </header>
    <div class="inventory-options">
      <DirectoryPicker
        v-model="cid"
        :account-id="accountId"
        :name="name"
        label="库存扫描目录"
        :disabled="op.busy.value || busyJob"
        @selected="chooseRoot"
      />
      <label
        >目录内容类型<select
          v-model="kindHint"
          aria-label="目录内容类型"
          :disabled="op.busy.value || busyJob"
        >
          <option value="auto">按目录分类判断</option>
          <option value="movie">电影目录</option>
          <option value="tv">剧集目录</option>
        </select></label
      >
    </div>
    <label class="checkbox-label"
      ><input v-model="enrich" type="checkbox" :disabled="busyJob || op.busy.value" />补充 TMDB
      海报与片名（每轮最多 200 个不同作品）</label
    >
    <p class="muted-text">
      支持 [tmdb-编号] 与 {tmdb-编号}
      目录标记。没有标记的作品保留为待核对；单独扫描作品目录时可手动选择电影或剧集类型。
    </p>
    <div class="action-row">
      <button
        class="button primary"
        :disabled="!accountId || busyJob || op.busy.value || op.uncertain.value"
        @click="scan"
      >
        {{ busyJob ? '库存扫描进行中' : '扫描盘内库存' }}</button
      ><button class="button" :disabled="catalogue.loading.value" @click="load">刷新库存</button
      ><button
        v-if="busyJob"
        class="button"
        :disabled="op.busy.value || op.uncertain.value"
        @click="stop"
      >
        停止库存扫描
      </button>
    </div>
    <OperationState :busy="op.busy.value" :error="op.error.value" :uncertain="op.uncertain.value" />
    <p v-if="catalogue.data.value?.has_snapshot" class="muted-text">
      上次完整扫描：{{ date(Number(catalogue.data.value.updated)) }} · 视频
      {{ summary.video_count ?? 0 }} 个 · 未归入作品目录
      {{ summary.unassigned_videos ?? 0 }} 个。目录标记不代表 Emby 已入库。
    </p>
    <p
      v-if="Number(summary.metadata_failed) || Number(summary.metadata_pending)"
      class="notice-bar"
    >
      海报补充失败 {{ summary.metadata_failed ?? 0 }} 个，达到本轮上限暂缓
      {{ summary.metadata_pending ?? 0 }} 个；库存目录结果仍可查看。
    </p>
  </section>
  <TaskReceipt v-if="job" :key="job" :id="job" @update="taskUpdated" />
  <section class="panel inventory-results">
    <div class="inventory-toolbar">
      <nav class="browse-tabs" aria-label="库存分类">
        <button
          v-for="(label, key) in { '': '全部', movie: '电影', tv: '剧集', unknown: '待确认' }"
          :key="key"
          :class="{ active: filter === key }"
          @click="filter = String(key)"
        >
          {{ label }} {{ counts[key === '' ? 'all' : key === 'unknown' ? '' : key] ?? 0 }}
        </button>
      </nav>
      <form class="inventory-search" @submit.prevent="search">
        <input
          v-model="keyword"
          aria-label="查找盘内作品"
          maxlength="100"
          placeholder="片名或网盘路径"
        /><button class="button" type="submit">查找</button>
      </form>
    </div>
    <LoadState
      :loading="catalogue.loading.value && !catalogue.data.value"
      :error="catalogue.error.value"
      :empty="!!catalogue.data.value && !items.length"
      :message="
        catalogue.data.value?.has_snapshot
          ? '没有符合条件的作品目录'
          : '尚无完整库存。选择目录后点击扫描盘内库存。'
      "
      @retry="load"
    />
    <div class="inventory-posters">
      <article v-for="row in items" :key="String(row.cid)" class="inventory-work">
        <PosterCard :card="card(row)" hide-status @open="selected = row" />
        <p>
          {{ kindLabel(row.kind) }} · {{ row.video_count }} 个视频<template
            v-if="row.kind === 'tv'"
          >
            · {{ row.episode_count }} 集已识别</template
          >
        </p>
        <small>{{ row.metadata_verified ? '已读取 TMDB 资料' : '目录信息待核对' }}</small
        ><button class="text-link" @click="selected = row">查看目录与操作</button>
      </article>
    </div>
    <div v-if="Number(catalogue.data.value?.total) > 48" class="pagination">
      <button
        class="button"
        :disabled="!offset || catalogue.loading.value"
        @click="
          offset = Math.max(0, offset - 48);
          load();
        "
      >
        上一页</button
      ><span
        >{{ offset + 1 }}–{{ Math.min(offset + 48, Number(catalogue.data.value?.total)) }} /
        {{ catalogue.data.value?.total }}</span
      ><button
        class="button"
        :disabled="!catalogue.data.value?.has_more || catalogue.loading.value"
        @click="
          offset += 48;
          load();
        "
      >
        下一页
      </button>
    </div>
  </section>
  <ModalDialog v-if="selected" :title="String(selected.title)" @close="selected = undefined">
    <p class="path-text">{{ selected.path }}</p>
    <p>
      {{ kindLabel(selected.kind) }} · {{ selected.video_count }} 个视频 · {{ size(selected.size) }}
    </p>
    <p>
      {{ selected.metadata_verified ? '已读取 TMDB 作品资料' : '身份来自目录名称，尚未核验 TMDB'
      }}<template v-if="selected.tmdb_id"> · TMDB {{ selected.tmdb_id }}</template>
    </p>
    <p v-if="selected.kind === 'tv'" class="notice-bar">
      识别到 {{ selected.episode_count }} 个季集；{{ selected.unrecognized_episodes }}
      个视频季集不明确。此处仅列出已有集，不据此判定整剧完整。
    </p>
    <div v-if="Array.isArray(selected.episodes)" class="inventory-episodes">
      <span v-for="ep in selected.episodes.slice(0, 200)" :key="String(ep)"
        >S{{ String((ep as number[])[0]).padStart(2, '0') }}E{{
          String((ep as number[])[1]).padStart(2, '0')
        }}</span
      >
      <p v-if="selected.episodes.length > 200">仅展示前 200 个季集。</p>
    </div>
    <details>
      <summary>网盘视频清单{{ selected.files_truncated ? '（前 100 个）' : '' }}</summary>
      <p v-for="file in records(selected.files)" :key="String(file.id)" class="path-text">
        {{ file.name }} · {{ size(file.size) }}
      </p>
    </details>
    <template #footer
      ><button
        class="button"
        @click="
          emit('browse', { cid: String(selected.cid), name: String(selected.name) });
          selected = undefined;
        "
      >
        浏览网盘目录</button
      ><RouterLink
        class="button"
        :to="{
          path: '/organize/workspace',
          query: {
            account: accountId,
            source_cid: String(selected.cid),
            source_name: String(selected.name),
          },
        }"
        >进入整理预览</RouterLink
      ><RouterLink
        class="button primary"
        :to="{
          path: '/media/strm',
          query: {
            account: accountId,
            source_cid: String(selected.cid),
            source_name: String(selected.name),
          },
        }"
        >去生成 STRM</RouterLink
      ></template
    >
  </ModalDialog>
</template>

<style scoped>
.inventory-controls,
.inventory-results {
  margin-top: 18px;
}
.inventory-options {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 20px;
  margin: 18px 0;
}
.inventory-options > label {
  display: grid;
  gap: 7px;
}
.inventory-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  justify-content: space-between;
  align-items: center;
}
.inventory-search {
  display: flex;
  gap: 8px;
}
.inventory-posters {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(145px, 1fr));
  gap: 22px 18px;
  margin-top: 24px;
}
.inventory-work {
  min-width: 0;
}
.inventory-work > p {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.6;
}
.inventory-work > small {
  display: block;
  color: var(--muted);
  margin: 7px 0;
}
.inventory-episodes {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 16px 0;
}
.inventory-episodes span {
  background: var(--surface2);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 12px;
}
@media (max-width: 600px) {
  .inventory-options {
    grid-template-columns: 1fr;
  }
  .inventory-search {
    width: 100%;
  }
  .inventory-search input {
    min-width: 0;
    flex: 1;
  }
  .inventory-posters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
