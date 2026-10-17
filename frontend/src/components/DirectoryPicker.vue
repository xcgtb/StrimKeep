<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { get, query } from '../api/client';
import { object, records, text } from '../api/domain';

const props = defineProps<{ accountId: string }>();
const emit = defineEmits<{ pick: [id: string, name: string] }>();

const cid = ref('0');
const trail = ref<{ id: string; name: string }[]>([{ id: '0', name: '根目录' }]);
const rows = ref<Record<string, unknown>[]>([]);
const loading = ref(false);
const error = ref('');
const open = ref(false);

async function load() {
  if (!props.accountId) return;
  loading.value = true;
  error.value = '';
  try {
    const data = object(
      await get<Record<string, unknown>>(
        query('/api/cloud/files', { account_id: props.accountId, cid: cid.value, offset: 0 }),
      ),
    );
    rows.value = records(data.items).filter((r) => r.is_dir === true);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
function enter(row: Record<string, unknown>) {
  trail.value = [...trail.value, { id: text(row.id), name: text(row.name) }];
  cid.value = text(row.id);
  void load();
}
function back() {
  if (trail.value.length <= 1) return;
  trail.value = trail.value.slice(0, -1);
  cid.value = trail.value[trail.value.length - 1]!.id;
  void load();
}
function toggle() {
  open.value = !open.value;
  if (open.value) void load();
}
function pickCurrent() {
  emit('pick', cid.value, trail.value[trail.value.length - 1]!.name);
  open.value = false;
}
watch(() => props.accountId, () => {
  trail.value = [{ id: '0', name: '根目录' }];
  cid.value = '0';
});
onMounted(() => void load());
</script>
<template>
  <span style="position: relative; display: inline-block">
    <button class="btn gray sm" type="button" @click="toggle">浏览目录</button>
    <div v-if="open" class="panel" style="position: absolute; z-index: 20; min-width: 320px; max-height: 360px; overflow: auto; margin-top: 6px">
      <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px">
        <button class="btn gray sm" :disabled="trail.length <= 1" @click="back">↑ 上级</button>
        <span class="hint">{{ trail[trail.length - 1]?.name }}</span>
        <button class="btn sm" type="button" @click="pickCurrent">选这个目录</button>
      </div>
      <p v-if="error" class="hint">{{ error }}</p>
      <div v-if="!rows.length && !loading" class="list-empty">此目录下没有子目录</div>
      <div v-for="row in rows" :key="text(row.id)" class="cw-file-row" style="cursor: pointer" @click="enter(row)">
        <span class="cw-file-name">▸ {{ text(row.name) }}</span>
      </div>
    </div>
  </span>
</template>
