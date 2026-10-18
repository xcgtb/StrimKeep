<script setup lang="ts">
import { watch } from 'vue';
import TaskChain from './TaskChain.vue';
import { readData, records, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useActivityUpdates } from '../composables/useActivityUpdates';
const props = defineProps<{ id: string }>();
const request = useRequest<Data>();
const labels: Record<string, string> = {
  queue: '排队与完成',
  transfer: '转存接收',
  recognize: '识别作品',
  probe: '媒体探测',
  naming: '文件命名',
  organize: '移动归档',
  strm: 'STRM 生成',
  emby: 'Emby 入库',
  notify: '消息通知',
  governance: '源文件治理',
  channel: '频道监控',
  maintenance: '账号维护',
};
function load() {
  if (!props.id || request.loading.value) return;
  return request.run((signal) =>
    readData(`/api/cloud/jobs/${encodeURIComponent(props.id)}/timeline`, signal),
  );
}
watch(
  () => props.id,
  () => {
    request.cancel();
    request.data.value = undefined;
    void load();
  },
  { immediate: true },
);
useActivityUpdates(load);
</script>
<template>
  <TaskChain :id="id" />
  <section class="business-timeline" aria-label="任务处理过程">
    <header class="section-heading">
      <h3>处理过程</h3>
      <RouterLink :to="{ path: '/logs', query: { task: id } }" class="text-link"
        >查看任务日志 →</RouterLink
      >
    </header>
    <p v-if="request.error.value" class="inline-error">{{ request.error.value }}</p>
    <p v-if="request.data.value?.truncated" class="data-caption">
      仅展示最近的过程记录，更早的事件请查看原任务回执。
    </p>
    <ol v-if="records(request.data.value?.items).length">
      <li
        v-for="row in records(request.data.value?.items)"
        :key="String(row.id)"
        :class="String(row.level).toLowerCase()"
      >
        <div class="timeline-stage">
          <strong>{{ labels[String(row.stage)] || row.stage }}</strong
          ><time>{{ String(row.ts).split('T')[1] }}</time
          ><span v-if="object(row.details).elapsed_ms != null"
            >{{ Number(object(row.details).elapsed_ms) / 1000 }} 秒</span
          >
        </div>
        <p>{{ row.message }}</p>
        <p
          v-if="
            object(row.details).reason &&
            !String(row.message).includes(String(object(row.details).reason))
          "
          class="warning-text"
        >
          {{ object(row.details).reason }}
        </p>
      </li>
    </ol>
    <p v-else-if="!request.loading.value && !request.error.value" class="muted-text">
      这条历史任务暂无过程日志，可查看已保存的执行回执。
    </p>
  </section>
</template>
