<script setup lang="ts">
import { onMounted } from 'vue';
import { readData, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import PageHeading from '../components/PageHeading.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
const runtime = useRequest<Data>(),
  storage = useRequest<Data>(),
  health = useRequest<Data>(),
  paths = useRequest<Data>(),
  consistency = useRequest<Data>();
async function load() {
  await Promise.allSettled([
    runtime.run((signal) => readData('/api/runtime/status', signal)),
    storage.run((signal) => readData('/api/storage/status', signal)),
    health.run((signal) => readData('/api/library/health', signal)),
    paths.run((signal) => readData('/api/health', signal)),
    consistency.run((signal) => readData('/api/consistency', signal)),
  ]);
}
onMounted(load);
useVisiblePoll(() => runtime.run((signal) => readData('/api/runtime/status', signal)), 15000);
</script>
<template>
  <PageHeading
    title="系统状态"
    subtitle="查看运行任务、存储健康、片库事实与一致性，不触发全库扫描。"
    ><button class="button" @click="load">更新状态</button></PageHeading
  >
  <div class="two-column">
    <section
      v-for="(state, label) in {
        服务与当前任务: runtime,
        数据存储: storage,
        片库健康: health,
        NAS路径与服务时区: paths,
        事实一致性: consistency,
      }"
      :key="label"
      class="panel"
    >
      <h2>{{ label }}</h2>
      <p v-if="state.error.value" class="inline-error">{{ state.error.value }}</p>
      <EvidenceDetails :value="state.data.value" />
    </section>
  </div>
</template>
