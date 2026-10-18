<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { FolderOpen, Save } from '@lucide/vue';
import { useCloud } from '../stores/cloud';
import { accountPath, readData, writeData, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import DirectoryPicker from './DirectoryPicker.vue';
import OperationState from './OperationState.vue';
const props = defineProps<{ roles: string[]; title?: string }>();
const emit = defineEmits<{ saved: [] }>();
const cloud = useCloud(),
  request = useRequest<Data>(),
  op = useOperation(() => cloud.id);
const draft = ref<Record<string, string | null>>({}),
  names = ref<Record<string, string>>({});
const labels: Record<string, string> = {
  source: '待整理目录',
  target: '整理归档目录',
  unknown: '未识别目录',
  transfer: '分享链接转存目录',
  magnet: '磁力下载目录',
  ed2k: 'ed2k 下载目录',
  instant: '秒传接收目录',
  bot: '机器人接收目录',
  channel: '频道默认接收目录',
  strm: 'STRM 手动同步来源',
};
const descriptions: Record<string, string> = {
  source: '扫描待整理视频及子目录。',
  target: '识别后按分类归档到此目录。',
  unknown: '识别失败的资源供人工核对。',
  transfer: '接收分享链接中的资源。',
  magnet: '115 云下载的磁力任务保存位置。',
  ed2k: '115 云下载的 ed2k 资源保存位置。',
  instant: '秒传资源的接收位置。',
  bot: '机器人接收资源使用此目录。',
  channel: '频道未单独指定目录时使用此位置。',
  strm: '只生成播放引用，不移动或重命名网盘源文件。',
};
const changed = computed(() =>
  props.roles.filter(
    (role) =>
      (draft.value[role] || null) !==
      (object(object(request.data.value?.directories)[role]).id || null),
  ),
);
async function load() {
  if (!cloud.id) return;
  const aid = cloud.id;
  const value = await request.run((signal) => readData(accountPath(aid, 'directories'), signal));
  if (value && aid === cloud.id) {
    const saved = object(value.directories);
    draft.value = Object.fromEntries(
      props.roles.map((role) => [
        role,
        object(saved[role]).id ? String(object(saved[role]).id) : null,
      ]),
    );
    names.value = Object.fromEntries(
      props.roles.map((role) => [role, String(object(saved[role]).name || '')]),
    );
    op.reconcile();
  }
}
async function save() {
  if (!request.data.value || !changed.value.length) return;
  const value = await op.run(() =>
    writeData(accountPath(cloud.id, 'directories'), {
      expected_revision: request.data.value?.revision,
      directories: Object.fromEntries(changed.value.map((role) => [role, draft.value[role]])),
    }),
  );
  if (value) {
    await load();
    emit('saved');
  }
}
watch(
  () => [cloud.id, props.roles.join(',')],
  () => {
    request.cancel();
    draft.value = {};
    names.value = {};
    void load();
  },
  { immediate: true },
);
</script>
<template>
  <section v-if="cloud.id" class="panel directory-role-card">
    <header class="section-heading">
      <div>
        <h2><FolderOpen :size="16" /> {{ title || '本模块目录' }}</h2>
        <p>仅保存本模块的目录，其他模块的选择保持原值。</p>
      </div>
      <button class="button small" :disabled="request.loading.value || op.busy.value" @click="load">
        重新读取目录
      </button>
    </header>
    <OperationState
      :busy="request.loading.value || op.busy.value"
      :error="request.error.value || op.error.value"
      :uncertain="op.uncertain.value"
    />
    <div class="directory-role-grid">
      <div v-for="role in roles" :key="role" class="dirfield directory-purpose-card">
        <DirectoryPicker
          :model-value="draft[role]"
          :account-id="cloud.id"
          :name="names[role]"
          :label="labels[role] || role"
          :disabled="op.busy.value || op.uncertain.value"
          @selected="
            (value) => {
              draft[role] = value.id;
              names[role] = value.name;
            }
          "
        />
        <p>{{ descriptions[role] }}</p>
        <button
          class="text-link tiny"
          :disabled="op.busy.value || op.uncertain.value"
          @click="
            draft[role] = null;
            names[role] = '';
          "
        >
          清空选择
        </button>
      </div>
    </div>
    <div class="actions mt18">
      <button
        class="button primary"
        :disabled="
          !changed.length ||
          !request.data.value ||
          !!request.error.value ||
          op.busy.value ||
          op.uncertain.value
        "
        @click="save"
      >
        <Save :size="14" />保存本模块目录</button
      ><span v-if="changed.length" class="muted-text">{{ changed.length }} 项目录待保存</span>
    </div>
  </section>
</template>
